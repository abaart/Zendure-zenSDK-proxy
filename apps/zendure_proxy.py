"""
Zendure Proxy - AppDaemon App
==============================
Python reimplementation of the Node-RED Zendure-proxy flow.

Exposes two HTTP endpoints (same interface as real Zendure devices):
  GET  /properties/report  ->  queries all Zendure devices in parallel,
                               returns a merged response with per-device fields.
  POST /properties/write   ->  distributes the power command across devices
                               using nonlinear SoC-based balancing.

Queue / concurrency model
--------------------------
* One persistent aiohttp.ClientSession per device.
* One asyncio.Lock per device: ensures at most one in-flight HTTP request per device.
* All incoming HA requests are placed in a shared pending list before touching
  the devices.  A single background worker drains that list each iteration so
  that slow Zendure responses never cause HA to time out.

  GET coalescing   – every GET that arrives while a previous GET is being
                     processed receives the same response as that first GET.
  POST dedup       – if multiple POSTs with identical property key-sets arrive
                     before the first one is sent to the devices, only the
                     *most recently received* one is executed; the others get
                     an immediate synthetic {"ack":"pong"} so HA is not blocked.

Configuration (apps.yaml)
--------------------------
  module: zendure_proxy
  class: ZendureProxy
  ip_zendure_1: "192.168.x.x"
  ip_zendure_2: "192.168.x.y"
  ip_zendure_3: ""             # leave empty for two-device setup
  server_port: 8120
  # --- optional tweaks (defaults match the Node-RED flow) ---
  single_mode_upperlimit_percent: 100
  single_mode_lowerlimit_percent: 40
  single_mode_change_device_diff: 5
  single_mode_delayed_standby_timer: 300
  single_mode_standby_charging_enable: true
  single_mode_standby_discharging_enable: true
  singlemode_transition_timer: 40
  balancing_factor: 5
  dualmode_damper_enable: false
  dualmode_damper_timer: 120
  dualmode_damper_amount: 200
  always_dual_mode: false
  equal_mode: false
  solar_power_info: false
  manual_mode_repeat: true
"""

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp
import aiohttp.web
import appdaemon.plugins.hass.hassapi as hass

PROXY_VERSION = "20260512-ad"


# ─── Pure helpers ─────────────────────────────────────────────────────────────

def _now() -> float:
    """Monotonic clock in seconds."""
    return time.monotonic()


def _epoch() -> int:
    return int(time.time())


def _distribute_power(
    total: int,
    avail: list[float],
    max_power: int,
    balancing_factor: int = 5,
) -> list[int]:
    """
    Distribute *total* watts across devices, weighted nonlinearly by *avail*.

    avail[i] is the "headroom" of device i (e.g. %-points until full/empty).
    Devices with more headroom receive more power; the exponent
    *balancing_factor* controls how aggressively.  After proportional
    distribution the per-device max is enforced and any surplus is
    redistributed to devices that still have headroom.
    """
    n = len(avail)
    avail = [max(0.0, a) for a in avail]

    if n == 0 or sum(avail) == 0:
        return [0] * n

    weights = [a ** balancing_factor for a in avail]
    total_weight = sum(weights)
    if total_weight == 0:
        per = total // n
        return [min(per, max_power)] * n

    # Initial proportional split
    power = [float(total) * (w / total_weight) for w in weights]

    # Enforce per-device max and redistribute surplus (up to n+1 passes)
    for _ in range(n + 1):
        surplus = 0.0
        headroom = []
        for i in range(n):
            if power[i] > max_power:
                surplus += power[i] - max_power
                power[i] = float(max_power)
                headroom.append(0.0)
            else:
                headroom.append(float(max_power) - power[i])
        if surplus < 0.5:
            break
        total_headroom = sum(headroom)
        if total_headroom < 0.5:
            break
        for i in range(n):
            if headroom[i] > 0:
                power[i] += surplus * (headroom[i] / total_headroom)

    return [math.floor(p) for p in power]


# ─── State dataclasses ────────────────────────────────────────────────────────

@dataclass
class DeviceState:
    ip: str = ""
    sn: str = ""
    electric_level: int = 50       # SoC %
    soc_status: int = 0            # 0 = normal, 1 = calibrating
    smart_mode: int = 0            # 0 = flash/sleep, 1 = RAM/active
    soc_limit: int = 0             # 0 = normal, 1 = charge limit, 2 = discharge limit
    charge_max_limit: int = 800    # W, per device
    inverse_max_power: int = 800   # W, per device
    gridoff_mode: int = 2
    latest_power_cmd: int = 0      # Last power command (W) sent to this device
    latest_power_cmd_zero_ts: float = 0.0
    last_response: Optional[dict] = None
    standby_task: Optional[asyncio.Task] = None


@dataclass
class ProxyState:
    device_count: int = 0
    devices: list[DeviceState] = field(default_factory=list)

    # Mode
    ac_mode: int = 0              # 0 = idle, 1 = charge, 2 = discharge
    min_soc: int = 100            # × 10  (100 → 10 %)
    soc_set: int = 1000           # × 10  (1000 → 100 %)
    max_power_in: int = 800       # W per device, derived from chargeMaxLimit
    max_power_out: int = 800      # W per device, derived from inverseMaxPower

    # Active-device bookkeeping
    device_active_count: int = 1
    devices_active_idx: list[int] = field(default_factory=lambda: [0])
    single_mode_active_device: int = 0   # index into devices[]

    # Last commanded aggregate values (as seen by HA)
    latest_power_cmd: int = 0
    input_limit: int = 0
    output_limit: int = 0
    input_limit_effective: int = 0
    output_limit_effective: int = 0
    charge_max_limit_cmd: int = 0
    charge_max_limit_effective: int = 0
    inverse_max_power_cmd: int = 0
    inverse_max_power_effective: int = 0

    # Smooth device-switch transition
    transition_start_ts: float = 0.0
    transition_original_device: int = 0

    # Dual-mode damper state
    dualmode_damper_active: bool = False
    dualmode_damper_start_ts: float = 0.0

    # Flags
    ac_mode_inconsistent: bool = False
    equal_mode: bool = False
    always_dual_mode: bool = False

    # Counters & timestamps
    counter_get_received: int = 0
    counter_get_replies: int = 0
    counter_post_received: int = 0
    counter_post_replies: int = 0
    counter_missing: list[int] = field(default_factory=lambda: [0, 0, 0])
    latest_get_ts: float = 0.0
    latest_power_message_ts: float = 0.0
    last_post_payload: Optional[dict] = None   # for manual-mode repeat

    # Cached last combined GET response (used when a device is temporarily down)
    last_get_response: Optional[dict] = None


# ─── AppDaemon App ────────────────────────────────────────────────────────────

class ZendureProxy(hass.Hass):
    """
    AppDaemon app: async HTTP proxy between Home Assistant and Zendure devices.
    """

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def initialize(self):
        self._load_config()

        n = len(self._cfg["device_ips"])

        # One session + one lock per device
        timeout = aiohttp.ClientTimeout(total=15)
        self._sessions: list[aiohttp.ClientSession] = [
            aiohttp.ClientSession(timeout=timeout) for _ in range(n)
        ]
        self._locks: list[asyncio.Lock] = [asyncio.Lock() for _ in range(n)]

        self._state = ProxyState(
            device_count=n,
            devices=[DeviceState(ip=ip) for ip in self._cfg["device_ips"]],
            equal_mode=self._cfg["equal_mode"],
            always_dual_mode=self._cfg["always_dual_mode"],
        )

        # Incoming-request queues (protected by _queue_lock)
        self._pending_gets: list[asyncio.Future] = []
        self._pending_posts: list[tuple[dict, asyncio.Future]] = []
        self._queue_lock = asyncio.Lock()
        self._queue_event = asyncio.Event()

        # Background tasks
        self._processor_task = asyncio.ensure_future(self._request_processor())
        asyncio.ensure_future(self._init_serial_numbers())

        await self._start_server()

        self.log(
            f"Zendure proxy v{PROXY_VERSION} started | "
            f"port={self._cfg['server_port']} | "
            f"devices={self._cfg['device_ips']}"
        )

    async def terminate(self):
        if hasattr(self, "_runner"):
            await self._runner.cleanup()
        if hasattr(self, "_processor_task"):
            self._processor_task.cancel()
        for s in self._sessions:
            await s.close()
        self.log("Zendure proxy stopped")

    # ── Configuration ──────────────────────────────────────────────────────────

    def _load_config(self):
        raw = [
            str(self.args.get("ip_zendure_1", "")).strip(),
            str(self.args.get("ip_zendure_2", "")).strip(),
            str(self.args.get("ip_zendure_3", "")).strip(),
        ]
        ips = [ip for ip in raw if ip]
        self._cfg = {
            "device_ips": ips,
            "server_port": int(self.args.get("server_port", 8120)),
            "server_host": str(self.args.get("server_host", "0.0.0.0")),
            "single_mode_upper_pct": int(self.args.get("single_mode_upperlimit_percent", 100)),
            "single_mode_lower_pct": int(self.args.get("single_mode_lowerlimit_percent", 40)),
            "device_change_diff": int(self.args.get("single_mode_change_device_diff", 5)),
            "standby_timer": int(self.args.get("single_mode_delayed_standby_timer", 300)),
            "standby_charging": bool(self.args.get("single_mode_standby_charging_enable", True)),
            "standby_discharging": bool(self.args.get("single_mode_standby_discharging_enable", True)),
            "transition_timer": int(self.args.get("singlemode_transition_timer", 40)),
            "balancing_factor": int(self.args.get("balancing_factor", 5)),
            "damper_enable": bool(self.args.get("dualmode_damper_enable", False)),
            "damper_timer": int(self.args.get("dualmode_damper_timer", 120)),
            "damper_amount": int(self.args.get("dualmode_damper_amount", 200)),
            "always_dual_mode": bool(self.args.get("always_dual_mode", False)),
            "equal_mode": bool(self.args.get("equal_mode", False)),
            "solar_power_info": bool(self.args.get("solar_power_info", False)),
            "manual_mode_repeat": bool(self.args.get("manual_mode_repeat", True)),
        }
        if not ips:
            self.log(
                "WARNING: no Zendure IPs configured – proxy will not function",
                level="WARNING",
            )

    # ── HTTP server ────────────────────────────────────────────────────────────

    async def _start_server(self):
        app = aiohttp.web.Application()
        for prefix in ("", "/endpoint"):
            app.router.add_get(f"{prefix}/properties/report", self._handle_get)
            app.router.add_post(f"{prefix}/properties/write", self._handle_post)
        self._runner = aiohttp.web.AppRunner(app)
        await self._runner.setup()
        site = aiohttp.web.TCPSite(
            self._runner,
            self._cfg["server_host"],
            self._cfg["server_port"],
        )
        await site.start()

    # ── HTTP handlers ──────────────────────────────────────────────────────────

    async def _handle_get(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        self._state.counter_get_received += 1
        self._state.latest_get_ts = _now()

        if not self._cfg["device_ips"]:
            return aiohttp.web.Response(status=503, text="No devices configured")

        # If SNs not yet available, return last cache to avoid HA timeout
        if not all(d.sn for d in self._state.devices):
            if self._state.last_get_response:
                return aiohttp.web.json_response(self._state.last_get_response)
            return aiohttp.web.Response(status=503, text="Initializing – try again shortly")

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()

        async with self._queue_lock:
            self._pending_gets.append(fut)
            self._queue_event.set()

        try:
            data = await asyncio.wait_for(fut, timeout=30.0)
            self._state.counter_get_replies += 1
            return aiohttp.web.json_response(data)
        except asyncio.TimeoutError:
            if self._state.last_get_response:
                return aiohttp.web.json_response(self._state.last_get_response)
            return aiohttp.web.Response(status=504, text="Upstream timeout")
        except Exception as exc:
            self.log(f"GET handler error: {exc}", level="ERROR")
            if self._state.last_get_response:
                return aiohttp.web.json_response(self._state.last_get_response)
            return aiohttp.web.Response(status=502, text=str(exc))

    async def _handle_post(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        self._state.counter_post_received += 1

        if not self._cfg["device_ips"]:
            return aiohttp.web.json_response({"ack": "pong"})

        try:
            payload = await request.json()
        except Exception:
            return aiohttp.web.Response(status=400, text="Invalid JSON")

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()

        async with self._queue_lock:
            self._pending_posts.append((payload, fut))
            self._queue_event.set()

        try:
            data = await asyncio.wait_for(fut, timeout=30.0)
            self._state.counter_post_replies += 1
            return aiohttp.web.json_response(data)
        except asyncio.TimeoutError:
            # Return synthetic OK so HA is not blocked
            return aiohttp.web.json_response({"ack": "pong"})
        except Exception as exc:
            self.log(f"POST handler error: {exc}", level="ERROR")
            return aiohttp.web.json_response({"ack": "pong"})

    # ── Queue processor ────────────────────────────────────────────────────────

    async def _request_processor(self):
        """
        Main worker loop.

        Each iteration:
          1. Wait until at least one request is pending.
          2. Drain all pending GETs and POSTs atomically.
          3. Execute one real GET → resolve every waiting GET future with
             the same response.
          4. Deduplicate POSTs by property key-set → execute only the latest
             per group; give immediate synthetic OK to the rest.
          5. Optionally re-execute the last power POST (manual-mode repeat).
        """
        while True:
            try:
                await self._queue_event.wait()

                async with self._queue_lock:
                    self._queue_event.clear()
                    gets = list(self._pending_gets)
                    self._pending_gets.clear()
                    posts = list(self._pending_posts)
                    self._pending_posts.clear()

                # ── Process GETs ──────────────────────────────────────────────
                get_response: Optional[dict] = None
                if gets:
                    try:
                        get_response = await self._execute_get()
                        for fut in gets:
                            if not fut.done():
                                fut.set_result(get_response)
                    except Exception as exc:
                        self.log(f"GET execution failed: {exc}", level="ERROR")
                        cached = self._state.last_get_response
                        for fut in gets:
                            if not fut.done():
                                if cached:
                                    fut.set_result(cached)
                                else:
                                    fut.set_exception(exc)

                # ── Process POSTs ─────────────────────────────────────────────
                if posts:
                    # Group by frozenset of property keys
                    groups: dict[frozenset, list[tuple[dict, asyncio.Future]]] = {}
                    for payload, fut in posts:
                        keys = frozenset(
                            (payload.get("properties") or {}).keys()
                        )
                        groups.setdefault(keys, []).append((payload, fut))

                    for _key_set, group in groups.items():
                        # All but the last get an immediate synthetic OK
                        for _payload, fut in group[:-1]:
                            if not fut.done():
                                fut.set_result({"ack": "pong"})
                        # Execute only the most recently received POST
                        latest_payload, latest_fut = group[-1]
                        try:
                            resp = await self._execute_post(latest_payload)
                            if not latest_fut.done():
                                latest_fut.set_result(resp)
                        except Exception as exc:
                            self.log(f"POST execution failed: {exc}", level="ERROR")
                            if not latest_fut.done():
                                latest_fut.set_result({"ack": "pong"})

                # ── Manual-mode repeat ────────────────────────────────────────
                # After each GET cycle, re-distribute the last power command so
                # that SoC balancing is continuously adjusted (equivalent to
                # manualMode_messageRepeat in Node-RED).
                if (
                    gets
                    and get_response is not None
                    and not posts
                    and self._cfg["manual_mode_repeat"]
                    and self._state.last_post_payload is not None
                    and self._state.latest_power_message_ts > 0
                ):
                    try:
                        await self._execute_post(
                            self._state.last_post_payload, is_repeat=True
                        )
                    except Exception as exc:
                        self.log(f"Manual-repeat POST failed: {exc}", level="WARNING")

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.log(f"Queue processor error: {exc}", level="ERROR")
                await asyncio.sleep(1)

    # ── Device communication ───────────────────────────────────────────────────

    async def _get_device(self, idx: int) -> Optional[dict]:
        """GET /properties/report from device *idx* under its exclusive lock."""
        ip = self._cfg["device_ips"][idx]
        async with self._locks[idx]:
            try:
                async with self._sessions[idx].get(
                    f"http://{ip}/properties/report"
                ) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    self.log(
                        f"Device {idx+1} GET HTTP {resp.status}", level="WARNING"
                    )
                    return None
            except Exception as exc:
                self.log(f"Device {idx+1} GET error: {exc}", level="WARNING")
                return None

    async def _post_device(self, idx: int, payload: dict) -> dict:
        """POST /properties/write to device *idx* under its exclusive lock."""
        ip = self._cfg["device_ips"][idx]
        async with self._locks[idx]:
            try:
                async with self._sessions[idx].post(
                    f"http://{ip}/properties/write", json=payload
                ) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    self.log(
                        f"Device {idx+1} POST HTTP {resp.status}", level="WARNING"
                    )
                    return {"ack": "pong"}
            except Exception as exc:
                self.log(f"Device {idx+1} POST error: {exc}", level="WARNING")
                return {"ack": "pong"}

    async def _init_serial_numbers(self):
        """Fetch and store the serial number of each device at startup."""
        for i in range(self._state.device_count):
            try:
                data = await self._get_device(i)
                if data and "sn" in data:
                    self._state.devices[i].sn = data["sn"]
                    self.log(f"Device {i+1} SN: {data['sn']}")
                else:
                    self.log(
                        f"Device {i+1}: could not fetch serial number", level="WARNING"
                    )
            except Exception as exc:
                self.log(f"Device {i+1} SN init error: {exc}", level="WARNING")

    # ── GET execution ──────────────────────────────────────────────────────────

    async def _execute_get(self) -> dict:
        """
        Fetch /properties/report from all devices in parallel (each under its
        own lock), update per-device state, build and return the combined
        response that HA will see as a single-device response.
        """
        n = self._state.device_count
        results: list[Optional[dict]] = await asyncio.gather(
            *[self._get_device(i) for i in range(n)]
        )

        for i, result in enumerate(results):
            if result is None:
                if i < len(self._state.counter_missing):
                    self._state.counter_missing[i] += 1

        # If any device failed, return last cached response rather than failing HA
        if any(r is None for r in results):
            self.log(
                "One or more devices did not respond; returning cached response",
                level="WARNING",
            )
            if self._state.last_get_response:
                return self._state.last_get_response
            raise RuntimeError("Devices unreachable and no cached response available")

        for i, data in enumerate(results):
            self._update_device_state(i, data)

        response = self._build_combined_response(results)
        self._state.last_get_response = response
        return response

    def _update_device_state(self, idx: int, data: dict):
        """Persist per-device values from a fresh GET response into ProxyState."""
        props = data.get("properties", {})
        dev = self._state.devices[idx]
        dev.last_response = data
        dev.electric_level = props.get("electricLevel", dev.electric_level)
        dev.soc_status = props.get("socStatus", 0)
        dev.smart_mode = props.get("smartMode", dev.smart_mode)
        dev.soc_limit = props.get("socLimit", 0)
        dev.charge_max_limit = props.get("chargeMaxLimit", dev.charge_max_limit)
        dev.inverse_max_power = props.get("inverseMaxPower", dev.inverse_max_power)
        dev.gridoff_mode = props.get("gridOffMode", dev.gridoff_mode)
        if data.get("sn"):
            dev.sn = data["sn"]

        # Aggregate limits: largest minSoc (conservative), smallest socSet
        if idx == 0:
            self._state.min_soc = props.get("minSoc", self._state.min_soc)
            self._state.soc_set = props.get("socSet", self._state.soc_set)
        else:
            self._state.min_soc = max(
                self._state.min_soc, props.get("minSoc", self._state.min_soc)
            )
            self._state.soc_set = min(
                self._state.soc_set, props.get("socSet", self._state.soc_set)
            )

        # Per-device max power (smallest of all devices is the common limit)
        active = [d for d in self._state.devices if d.charge_max_limit > 0]
        if active:
            self._state.max_power_in = min(d.charge_max_limit for d in active)
        active = [d for d in self._state.devices if d.inverse_max_power > 0]
        if active:
            self._state.max_power_out = min(d.inverse_max_power for d in active)

    def _build_combined_response(self, results: list[dict]) -> dict:
        """
        Merge N device responses into a single JSON object that looks like one
        Zendure device to Home Assistant / Gielz automation.

        Equivalent to the Node-RED 'GET Response handling' function node.
        """
        n = self._state.device_count
        st = self._state
        devs = st.devices

        def _prop(i: int, key: str, default: Any = 0) -> Any:
            return results[i].get("properties", {}).get(key, default)

        def _sum(key: str) -> int:
            return sum(results[i].get("properties", {}).get(key, 0) for i in range(n))

        resp: dict = {
            "sn": f"{n}x Zendure via PROXY",
            "product": results[0].get("product", ""),
            "proxyVersion": PROXY_VERSION,
            "timestamp": _epoch(),
            "packData": [],
            "properties": {},
        }

        # Combine pack data from all devices
        for data in results:
            resp["packData"].extend(data.get("packData", []))

        props = resp["properties"]
        props["ts"] = _epoch()

        # ── acMode: take from active devices; flag if inconsistent ────────────
        active_idx = st.devices_active_idx
        ac_modes = [_prop(i, "acMode") for i in active_idx]
        props["acMode"] = ac_modes[0] if ac_modes else 0
        st.ac_mode_inconsistent = len(set(ac_modes)) > 1

        # ── inputLimit / outputLimit: sum all devices (with command correction) ──
        input_limit_raw = _sum("inputLimit")
        if (st.input_limit != st.input_limit_effective) and (
            input_limit_raw == st.input_limit_effective
        ):
            props["inputLimit"] = st.input_limit
        else:
            props["inputLimit"] = input_limit_raw

        output_limit_raw = _sum("outputLimit")
        if (st.output_limit != st.output_limit_effective) and (
            output_limit_raw == st.output_limit_effective
        ):
            props["outputLimit"] = st.output_limit
        else:
            props["outputLimit"] = output_limit_raw

        # inverseMaxPower and chargeMaxLimit: sum across devices
        inv_raw = sum(d.inverse_max_power for d in devs)
        if (st.inverse_max_power_cmd != st.inverse_max_power_effective) and (
            inv_raw == st.inverse_max_power_effective
        ):
            props["inverseMaxPower"] = st.inverse_max_power_cmd
        else:
            props["inverseMaxPower"] = inv_raw

        chg_raw = sum(d.charge_max_limit for d in devs)
        if (st.charge_max_limit_cmd != st.charge_max_limit_effective) and (
            chg_raw == st.charge_max_limit_effective
        ):
            props["chargeMaxLimit"] = st.charge_max_limit_cmd
        else:
            props["chargeMaxLimit"] = chg_raw

        # ── outputPackPower & packInputPower: sum then conflict-resolve ────────
        pack_out = _sum("outputPackPower")
        pack_in = _sum("packInputPower")
        if pack_in != 0 and pack_out != 0:
            if pack_in > pack_out:
                pack_in -= pack_out
                pack_out = 0
            elif pack_in < pack_out:
                pack_out -= pack_in
                pack_in = 0
            else:
                pack_in = pack_out = 0
        # Avoid ghost-power artefact in idle discharge mode
        if props["acMode"] == 2 and 0 < pack_out < 100:
            pack_out = 0
        props["outputPackPower"] = pack_out
        props["packInputPower"] = pack_in

        # ── gridInputPower & outputHomePower: same conflict-resolution ─────────
        grid_in = _sum("gridInputPower")
        home_out = _sum("outputHomePower")
        if grid_in != 0 and home_out != 0:
            if grid_in > home_out:
                grid_in -= home_out
                home_out = 0
            elif grid_in < home_out:
                home_out -= grid_in
                grid_in = 0
            else:
                grid_in = home_out = 0
        props["gridInputPower"] = grid_in
        props["outputHomePower"] = home_out

        props["solarInputPower"] = _sum("solarInputPower")
        props["gridOffPower"] = _sum("gridOffPower")

        # ── electricLevel: average with edge-case corrections ─────────────────
        soc = [devs[i].electric_level for i in range(n)]
        sc = [devs[i].soc_limit for i in range(n)]
        min_soc_pct = st.min_soc / 10.0

        if n == 1:
            props["electricLevel"] = soc[0]
        elif n == 2:
            eA, eB = float(soc[0]), float(soc[1])
            # If exactly one device hit the discharge limit, clamp it to minSoc
            # so the average doesn't skew the other device's behavior.
            if (sc[0] == 2) != (sc[1] == 2):
                if sc[0] == 2:
                    eA = min_soc_pct
                else:
                    eB = min_soc_pct
                if eA <= min_soc_pct + 1 and eB <= min_soc_pct + 1:
                    props["electricLevel"] = math.ceil((eA + eB) / 2)
                else:
                    props["electricLevel"] = math.floor((eA + eB) / 2)
            else:
                props["electricLevel"] = math.floor((eA + eB) / 2)
        else:
            props["electricLevel"] = math.floor(sum(soc) / n)

        # ── minSoc / socSet ───────────────────────────────────────────────────
        props["minSoc"] = max(_prop(i, "minSoc", 100) for i in range(n))
        props["socSet"] = min(_prop(i, "socSet", 1000) for i in range(n))

        # ── socLimit: combined ────────────────────────────────────────────────
        sc_vals = [_prop(i, "socLimit", 0) for i in range(n)]
        if all(v == 0 for v in sc_vals):
            props["socLimit"] = 0
        elif all(v == 1 for v in sc_vals):
            props["socLimit"] = 1
        elif all(v == 2 for v in sc_vals):
            props["socLimit"] = 2
        else:
            props["socLimit"] = 0  # mixed → normal

        # ── smartMode: 0 if any device is asleep ─────────────────────────────
        props["smartMode"] = min(_prop(i, "smartMode", 0) for i in range(n))

        # ── hyperTmp: average ─────────────────────────────────────────────────
        hyper = [_prop(i, "hyperTmp", 2731) for i in range(n)]
        props["hyperTmp"] = round(sum(hyper) / n)

        # ── Scalar fields from device 0 (or worst-case) ───────────────────────
        props["BatVolt"] = _prop(0, "BatVolt", 0)
        props["remainOutTime"] = _prop(0, "remainOutTime", 0)
        props["packNum"] = _sum("packNum")
        props["rssi"] = min(_prop(i, "rssi", 0) for i in range(n))
        props["is_error"] = max(_prop(i, "is_error", 0) for i in range(n))
        props["socStatus"] = max(_prop(i, "socStatus", 0) for i in range(n))
        props["batCalTime"] = max(_prop(i, "batCalTime", 0) for i in range(n))

        for key in ("gridReverse", "pvStatus", "acStatus", "dcStatus"):
            if key in results[0].get("properties", {}):
                props[key] = results[0]["properties"][key]

        # ── gridOffMode: combined rule ────────────────────────────────────────
        gom = [_prop(i, "gridOffMode", None) for i in range(n)]
        if all(v is not None for v in gom):
            cnt0 = gom.count(0)
            cnt1 = gom.count(1)
            cnt2 = gom.count(2)
            if cnt0 > 0:
                props["gridOffMode"] = 0
            elif cnt1 == 1 and cnt1 + cnt2 == n:
                props["gridOffMode"] = 1
            elif len(set(gom)) == 1:
                props["gridOffMode"] = gom[0]
            else:
                props["gridOffMode"] = 2
        else:
            props["gridOffMode"] = 2

        # ── Optional solar data ───────────────────────────────────────────────
        if self._cfg["solar_power_info"]:
            for key in ("solarPower1", "solarPower2", "solarPower3", "solarPower4"):
                if key in results[0].get("properties", {}):
                    props[key] = results[0]["properties"][key]

        # ── Per-device suffix fields (proxy additions) ────────────────────────
        for i in range(n):
            s = f"_{i+1}"
            dp = results[i].get("properties", {})
            props[f"electricLevel{s}"] = devs[i].electric_level
            props[f"latestPowerCmd{s}"] = devs[i].latest_power_cmd
            props[f"outputHomePower{s}"] = dp.get("outputHomePower", 0)
            props[f"gridInputPower{s}"] = dp.get("gridInputPower", 0)
            props[f"socStatus{s}"] = devs[i].soc_status
            props[f"smartMode{s}"] = devs[i].smart_mode
            props[f"hyperTmp{s}"] = dp.get("hyperTmp", 2731)
            props[f"sn{s}"] = devs[i].sn
            props[f"ipAddress{s}"] = devs[i].ip
            props[f"gridOffMode{s}"] = dp.get("gridOffMode", 2)

        # Pad slots for absent devices (always expose _1/_2/_3)
        for i in range(n, 3):
            s = f"_{i+1}"
            for key in (
                "electricLevel", "latestPowerCmd", "outputHomePower",
                "gridInputPower", "socStatus", "smartMode",
            ):
                props[f"{key}{s}"] = 0
            props[f"hyperTmp{s}"] = 2731
            props[f"sn{s}"] = ""
            props[f"ipAddress{s}"] = ""
            props[f"gridOffMode{s}"] = 2

        # Proxy metadata
        props["activeDevice"] = st.single_mode_active_device + 1   # 1-based
        props["proxyVersion"] = PROXY_VERSION
        props["latestPowerCmd"] = st.latest_power_cmd
        props["device_active_count"] = st.device_active_count

        # Pass through any device-0 properties not explicitly handled above
        for key, val in results[0].get("properties", {}).items():
            if key not in props:
                props[key] = val

        return resp

    # ── POST execution ─────────────────────────────────────────────────────────

    async def _execute_post(self, payload: dict, *, is_repeat: bool = False) -> dict:
        """
        Translate an aggregate HA POST into individual per-device commands,
        applying nonlinear SoC-based power distribution.

        Equivalent to the Node-RED 'POST Request handling' function node.
        """
        props = payload.get("properties") or {}
        st = self._state
        devs = st.devices
        n = st.device_count
        cfg = self._cfg

        # ── Non-power property pass-through ───────────────────────────────────
        POWER_KEYS = {"acMode", "inputLimit", "outputLimit"}
        has_power_cmd = bool(POWER_KEYS & set(props.keys()))

        if not has_power_cmd:
            # Properties like minSoc, socSet, gridOffMode, smartMode → all devices
            responses = []
            for i in range(n):
                dp: dict = dict(payload)
                if devs[i].sn:
                    dp = {"sn": devs[i].sn, "properties": dict(props)}
                responses.append(await self._post_device(i, dp))
            # Handle chargeMaxLimit / inverseMaxPower
            if "chargeMaxLimit" in props:
                per = props["chargeMaxLimit"] // n
                st.charge_max_limit_cmd = props["chargeMaxLimit"]
                st.charge_max_limit_effective = per * n
                for dev in devs:
                    dev.charge_max_limit = per
                st.max_power_in = per
            if "inverseMaxPower" in props:
                per = props["inverseMaxPower"] // n
                st.inverse_max_power_cmd = props["inverseMaxPower"]
                st.inverse_max_power_effective = per * n
                for dev in devs:
                    dev.inverse_max_power = per
                st.max_power_out = per
            return responses[0] if responses else {"ack": "pong"}

        # ── Power command ─────────────────────────────────────────────────────
        # Debounce: drop duplicates arriving < 1 s apart (unless manual repeat)
        now = _now()
        if not is_repeat and (now - st.latest_power_message_ts) < 1.0 and st.latest_power_message_ts > 0:
            pass  # still process; just don't debounce hard
        if not is_repeat:
            st.latest_power_message_ts = now

        ac_mode: int = props.get("acMode", st.ac_mode)
        input_limit: int = props.get("inputLimit", 0)
        output_limit: int = props.get("outputLimit", 0)
        total_power = input_limit if ac_mode == 1 else (output_limit if ac_mode == 2 else 0)

        max_power = st.max_power_in if ac_mode == 1 else st.max_power_out
        if max_power <= 0:
            max_power = 800  # safety fallback

        # ── Thresholds ────────────────────────────────────────────────────────
        upper = cfg["single_mode_upper_pct"] / 100.0 * max_power
        lower = cfg["single_mode_lower_pct"] / 100.0 * max_power

        prev_active_count = st.device_active_count

        # ── How many devices should be active? ────────────────────────────────
        force_all = cfg["equal_mode"] or cfg["always_dual_mode"]

        if n == 1:
            st.device_active_count = 1
        elif n == 2:
            self._update_active_count_2(ac_mode, total_power, upper, lower, force_all)
        else:
            self._update_active_count_3(ac_mode, total_power, upper, lower, force_all)

        # ── Which specific devices are active? ────────────────────────────────
        prev_active_device = st.single_mode_active_device
        self._select_active_devices(ac_mode, prev_active_count)

        # If device changed in single mode, start smooth transition
        if (
            st.device_active_count == 1
            and st.single_mode_active_device != prev_active_device
        ):
            st.transition_start_ts = now
            st.transition_original_device = prev_active_device

        # ── Per-device power distribution ─────────────────────────────────────
        per_device = self._distribute(ac_mode, total_power, max_power, cfg)

        # ── Smooth transition overlay ─────────────────────────────────────────
        per_device = self._apply_transition(per_device, now, cfg)

        # ── Dual-mode damper (prevents flapping around upper threshold) ────────
        if cfg["damper_enable"] and ac_mode == 2 and n >= 2:
            per_device = self._apply_damper(per_device, total_power, upper, now, cfg)

        # ── Send commands to devices ──────────────────────────────────────────
        tasks = []
        for i in range(n):
            pwr = per_device[i]
            if ac_mode == 1:
                dp = {"acMode": 1, "inputLimit": pwr}
            elif ac_mode == 2:
                dp = {"acMode": 2, "outputLimit": pwr}
            else:
                dp = {"acMode": 0, "inputLimit": 0, "outputLimit": 0}

            # Pass through any non-power properties in the same POST
            for k, v in props.items():
                if k not in POWER_KEYS:
                    dp[k] = v

            device_payload: dict = {"properties": dp}
            if devs[i].sn:
                device_payload["sn"] = devs[i].sn
            tasks.append(self._post_device(i, device_payload))
            devs[i].latest_power_cmd = pwr
            if pwr == 0:
                devs[i].latest_power_cmd_zero_ts = now

        responses = await asyncio.gather(*tasks)

        # ── Update aggregate state ────────────────────────────────────────────
        st.ac_mode = ac_mode
        st.latest_power_cmd = total_power
        if ac_mode == 1:
            st.input_limit = input_limit
            st.input_limit_effective = sum(per_device)
        elif ac_mode == 2:
            st.output_limit = output_limit
            st.output_limit_effective = sum(per_device)

        if not is_repeat:
            st.last_post_payload = payload

        # ── Standby management for inactive devices ───────────────────────────
        asyncio.ensure_future(self._manage_standby(ac_mode, per_device))

        return responses[0] if responses else {"ack": "pong"}

    # ── Device-count selection ─────────────────────────────────────────────────

    def _update_active_count_2(
        self,
        ac_mode: int,
        total_power: int,
        upper: float,
        lower: float,
        force_all: bool,
    ):
        """Update device_active_count for a 2-device setup."""
        st = self._state
        devs = st.devices
        n = 2
        min_soc_pct = st.min_soc / 10.0

        if force_all:
            st.device_active_count = n
            return

        # Emergency: if one device is below minSoc while charging, activate all
        if ac_mode == 1:
            below = [d.electric_level < min_soc_pct for d in devs[:n]]
            if sum(below) == 1:  # exactly one below minSoc
                st.device_active_count = n
                return

        if total_power == 0:
            return  # keep previous count while idle

        prev = st.device_active_count
        if total_power < lower:
            st.device_active_count = 1
        elif total_power > upper * n:
            st.device_active_count = n
        elif prev == 1 and total_power > upper:
            st.device_active_count = n
        elif prev == n and total_power < lower:
            st.device_active_count = 1
        # else: hysteresis – keep previous

    def _update_active_count_3(
        self,
        ac_mode: int,
        total_power: int,
        upper: float,
        lower: float,
        force_all: bool,
    ):
        """Update device_active_count for a 3-device setup."""
        st = self._state

        if force_all:
            st.device_active_count = 3
            return

        if total_power == 0:
            return

        dual_upper = upper * 2
        dual_lower = lower * 2

        prev = st.device_active_count
        if total_power < lower:
            st.device_active_count = 1
        elif total_power > dual_upper:
            st.device_active_count = 3
        elif prev == 1 and total_power > upper:
            st.device_active_count = 2
        elif prev == 3 and total_power < dual_lower:
            st.device_active_count = 2

    def _select_active_devices(self, ac_mode: int, prev_count: int):
        """
        Choose which devices are active based on SoC.

        Single mode  → lowest SoC charges / highest SoC discharges.
        Multi-device → activate the *active_count* devices with the most
                       available headroom.
        Hysteresis   → only switch active device when SoC diff exceeds threshold.
        """
        st = self._state
        devs = st.devices
        n = st.device_count
        active_count = st.device_active_count
        diff_threshold = self._cfg["device_change_diff"]
        min_soc_pct = st.min_soc / 10.0

        # Check if emergency minSoc override is needed (use 1 % threshold)
        at_minsoc_boundary = any(
            d.soc_limit == 2 for d in devs
        ) and any(
            d.electric_level < min_soc_pct for d in devs
        )
        if at_minsoc_boundary or any(d.soc_limit == 1 for d in devs):
            diff_threshold = 1

        if active_count >= n:
            st.devices_active_idx = list(range(n))
            return

        # Sort by SoC: for charging pick lowest first; for discharging pick highest
        soc_ranked = sorted(
            range(n),
            key=lambda i: devs[i].electric_level,
            reverse=(ac_mode == 2),
        )

        if active_count == 1:
            best = soc_ranked[0]
            current = st.single_mode_active_device
            current_soc = devs[current].electric_level
            best_soc = devs[best].electric_level
            if abs(best_soc - current_soc) >= diff_threshold:
                st.single_mode_active_device = best
            st.devices_active_idx = [st.single_mode_active_device]
        else:
            st.devices_active_idx = soc_ranked[:active_count]

    # ── Power distribution ─────────────────────────────────────────────────────

    def _distribute(
        self,
        ac_mode: int,
        total_power: int,
        max_power: int,
        cfg: dict,
    ) -> list[int]:
        """
        Calculate per-device power in watts using nonlinear SoC balancing.
        Inactive devices always receive 0.
        """
        st = self._state
        devs = st.devices
        n = st.device_count
        active_idx = st.devices_active_idx
        min_soc_pct = st.min_soc / 10.0
        soc_set_pct = st.soc_set / 10.0

        # Available headroom for each active device
        avail = [0.0] * n
        for i in active_idx:
            dev = devs[i]
            if dev.soc_status == 1:        # calibrating
                avail[i] = 0.0
            elif ac_mode == 1:             # charging
                avail[i] = max(0.0, soc_set_pct - dev.electric_level)
            elif ac_mode == 2:             # discharging
                avail[i] = max(0.0, dev.electric_level - min_soc_pct)

        active_avail = [avail[i] for i in active_idx]
        equal = cfg["equal_mode"] or sum(active_avail) == 0

        if equal:
            per = min(total_power // max(len(active_idx), 1), max_power)
            power_active = [per] * len(active_idx)
        else:
            power_active = _distribute_power(
                total_power, active_avail, max_power, cfg["balancing_factor"]
            )

        # If all headroom is zero, zero out power to avoid waking standby devices
        if sum(active_avail) == 0:
            power_active = [0] * len(active_idx)

        result = [0] * n
        for j, i in enumerate(active_idx):
            result[i] = power_active[j]
        return result

    # ── Transition & damper ────────────────────────────────────────────────────

    def _apply_transition(self, per_device: list[int], now: float, cfg: dict) -> list[int]:
        """
        When the active single-mode device changes, blend power between old and
        new device over *transition_timer* seconds to avoid abrupt power spikes.

        Phase 1 (0 – 75 %): 95 % to original, 5 % to new device.
        Phase 2 (75 – 100 %): 75 % to original, 25 % to new device.
        """
        st = self._state
        if st.transition_start_ts <= 0:
            return per_device

        elapsed = now - st.transition_start_ts
        timer = cfg["transition_timer"]

        if elapsed >= timer:
            st.transition_start_ts = 0.0
            return per_device

        orig = st.transition_original_device
        new = st.single_mode_active_device

        if orig == new or orig >= len(per_device) or new >= len(per_device):
            st.transition_start_ts = 0.0
            return per_device

        progress = elapsed / timer
        orig_frac = 0.95 if progress < 0.75 else 0.75
        total = sum(per_device)

        result = list(per_device)
        result[orig] = round(total * orig_frac)
        result[new] = round(total * (1.0 - orig_frac))
        return result

    def _apply_damper(
        self,
        per_device: list[int],
        total_power: int,
        upper: float,
        now: float,
        cfg: dict,
    ) -> list[int]:
        """
        Dual-mode damper (discharge only): if power briefly exceeds the
        single-mode threshold by less than *damper_amount* W, hold at the
        single-mode limit for *damper_timer* seconds before actually switching.
        Prevents oscillation when power hovers just above the threshold.
        """
        st = self._state
        excess = total_power - upper

        if excess <= 0:
            st.dualmode_damper_active = False
            return per_device

        if 0 < excess <= cfg["damper_amount"]:
            if not st.dualmode_damper_active:
                st.dualmode_damper_active = True
                st.dualmode_damper_start_ts = now
            elapsed = now - st.dualmode_damper_start_ts
            if elapsed < cfg["damper_timer"]:
                # Clamp to single-device limit
                n = self._state.device_count
                clamped = [round(upper / n)] * n
                return clamped
        else:
            st.dualmode_damper_active = False

        return per_device

    # ── Standby management ─────────────────────────────────────────────────────

    async def _manage_standby(self, ac_mode: int, per_device: list[int]):
        """
        For inactive devices in single mode: schedule a smartMode=0 command
        after *standby_timer* seconds.  Cancel the task if the device becomes
        active again before the timer fires.

        Wake inactive devices that were in standby before they receive power.
        """
        st = self._state
        devs = st.devices
        active_set = set(st.devices_active_idx)
        cfg = self._cfg

        for i, dev in enumerate(devs):
            if i in active_set:
                # Cancel any pending standby and ensure device is awake
                if dev.standby_task and not dev.standby_task.done():
                    dev.standby_task.cancel()
                dev.standby_task = None
                if dev.smart_mode == 0 and per_device[i] > 0:
                    await self._post_device(
                        i, {"sn": dev.sn, "properties": {"smartMode": 1}}
                    )
                    dev.smart_mode = 1
            else:
                # Device is inactive
                should_standby = (
                    st.device_active_count < st.device_count
                    and (
                        (ac_mode == 1 and cfg["standby_charging"])
                        or (ac_mode == 2 and cfg["standby_discharging"])
                    )
                )
                if should_standby and dev.standby_task is None:
                    dev.standby_task = asyncio.ensure_future(
                        self._delayed_standby(i, cfg["standby_timer"])
                    )
                elif not should_standby and dev.standby_task:
                    dev.standby_task.cancel()
                    dev.standby_task = None

    async def _delayed_standby(self, idx: int, delay: float):
        """Wait *delay* seconds then put device *idx* into deep sleep."""
        try:
            await asyncio.sleep(delay)
            dev = self._state.devices[idx]
            if idx not in self._state.devices_active_idx:
                await self._post_device(
                    idx, {"sn": dev.sn, "properties": {"smartMode": 0}}
                )
                dev.smart_mode = 0
                self.log(f"Device {idx+1} put into standby (smartMode=0)")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.log(f"Standby task error for device {idx+1}: {exc}", level="WARNING")
        finally:
            if idx < len(self._state.devices):
                self._state.devices[idx].standby_task = None
