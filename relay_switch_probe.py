#!/usr/bin/env python3
"""Probe Zendure relay switch detection options on one physical Zendure.

The script talks directly to one Zendure local API:

    GET  /properties/report
    POST /properties/write

It prints the raw GET fields and several derived relay-active options while it
sends simple charge/discharge/zero commands. Use the printed timestamps while
listening for the physical relay click.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
import json
import sys
import time
from typing import Any, Callable
from urllib import error, parse, request


PACK_STATE_LABELS = {
    0: "standby",
    1: "charging",
    2: "discharging",
}

AC_STATUS_LABELS = {
    0: "stopped",
    1: "running",
    2: "charging",
}


def main() -> int:
    args = parse_args()
    base_url = normalize_base_url(args.ip)

    print(f"Zendure relay switch probe")
    print(f"Device API: {base_url}")
    print(f"Mode: {args.mode}")
    print(f"Power command: {args.power} W")
    print(f"Power thresholds: on >= {args.power_on_threshold} W, off <= {args.power_off_threshold} W")
    print("")

    first_report = get_report(base_url, args.timeout)
    sn = args.sn or str(first_report.get("sn") or "").strip()
    if not sn and args.mode != "observe":
        raise SystemExit(
            "No serial number found in GET response. Run again with --sn <serial>."
        )

    print("Initial GET:")
    print_report_summary(first_report)
    print("")

    detectors = make_detectors(args)
    csv_file = open(args.csv, "w", newline="", encoding="utf-8") if args.csv else None
    csv_writer = make_csv_writer(csv_file) if csv_file else None
    last_ac_mode = int_value(first_report.get("properties", {}).get("acMode"), 1)

    try:
        if args.mode == "observe":
            sample_for(
                base_url,
                args.duration,
                args.sample_interval,
                "OBSERVE",
                detectors,
                args,
                csv_writer,
            )
            return 0

        if not args.yes and not confirm_run(args):
            print("Stopped before sending POST commands.")
            return 2

        for cycle in range(1, args.cycles + 1):
            print("")
            print(f"=== Cycle {cycle}/{args.cycles} ===")
            for phase in build_sequence(args):
                last_ac_mode = phase.ac_mode
                payload_props = phase.payload_props(args.smart_mode)
                print("")
                print(
                    f"POST {phase.name}: acMode={phase.ac_mode} "
                    f"inputLimit={payload_props.get('inputLimit', 0)} "
                    f"outputLimit={payload_props.get('outputLimit', 0)}"
                )
                print("Listen now. The next samples show which detector option changed.")
                if args.dry_run:
                    print("DRY RUN: POST not sent.")
                else:
                    post_properties(base_url, sn, payload_props, args.timeout)
                sample_for(
                    base_url,
                    phase.hold_seconds,
                    args.sample_interval,
                    phase.name,
                    detectors,
                    args,
                    csv_writer,
                )
    except KeyboardInterrupt:
        print("")
        print("Interrupted. Sending final zero command unless --no-final-zero was used.")
        if not args.no_final_zero and not args.dry_run and sn:
            send_final_zero(base_url, sn, last_ac_mode, args)
        return 130
    finally:
        if csv_file:
            csv_file.close()

    if not args.no_final_zero and not args.dry_run:
        print("")
        print("Sending final zero command.")
        send_final_zero(base_url, sn, last_ac_mode, args)

    print("")
    print("Detector switch counts:")
    for detector in detectors:
        print(f"- {detector.name}: {detector.switches}")
    if args.csv:
        print(f"CSV written: {args.csv}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Send simple commands to one Zendure and print GET-based relay "
            "detection options while you listen for relay clicks."
        )
    )
    parser.add_argument(
        "--ip",
        required=True,
        help="Zendure IP/host, or full base URL such as http://192.168.1.50",
    )
    parser.add_argument("--sn", help="Serial number. Default: read top-level sn from GET.")
    parser.add_argument(
        "--mode",
        choices=("observe", "charge", "discharge", "both", "direct-switch"),
        default="charge",
        help="Test sequence. Default: charge.",
    )
    parser.add_argument("--power", type=int, default=100, help="Commanded W. Default: 100.")
    parser.add_argument("--cycles", type=int, default=1, help="Number of cycles. Default: 1.")
    parser.add_argument(
        "--hold",
        type=float,
        default=8.0,
        help="Seconds to hold each nonzero command. Default: 8.",
    )
    parser.add_argument(
        "--zero-hold",
        type=float,
        default=8.0,
        help="Seconds to hold each zero command. Default: 8.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=60.0,
        help="Observe-mode duration in seconds. Default: 60.",
    )
    parser.add_argument(
        "--sample-interval",
        type=float,
        default=1.0,
        help="GET polling interval in seconds. Default: 1.",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout. Default: 5.")
    parser.add_argument(
        "--power-on-threshold",
        type=int,
        default=20,
        help="Measured W threshold for power_hyst ON. Default: 20.",
    )
    parser.add_argument(
        "--power-off-threshold",
        type=int,
        default=5,
        help="Measured W threshold for power_hyst OFF. Default: 5.",
    )
    parser.add_argument(
        "--required-samples",
        type=int,
        default=2,
        help="Samples needed before a detector accepts a change. Default: 2.",
    )
    parser.add_argument(
        "--csv",
        help="Optional CSV output path with all GET samples and detector states.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Do not ask for interactive confirmation before POST commands.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the sequence and poll GET, but do not send POST commands.",
    )
    parser.add_argument(
        "--no-smart-mode",
        dest="smart_mode",
        action="store_false",
        help="Do not include smartMode=1 in POST payloads.",
    )
    parser.set_defaults(smart_mode=True)
    parser.add_argument(
        "--no-final-zero",
        action="store_true",
        help="Do not send a final zero command at the end or on Ctrl+C.",
    )
    return parser.parse_args()


@dataclass
class Phase:
    name: str
    ac_mode: int
    input_limit: int
    output_limit: int
    hold_seconds: float

    def payload_props(self, smart_mode: bool) -> dict[str, int]:
        props = {
            "acMode": self.ac_mode,
            "inputLimit": self.input_limit,
            "outputLimit": self.output_limit,
        }
        if smart_mode:
            props["smartMode"] = 1
        return props


@dataclass
class Detector:
    name: str
    evaluate: Callable[[dict[str, Any], bool | None], bool | None]
    required_samples: int
    active: bool | None = None
    pending: bool | None = None
    pending_count: int = 0
    switches: int = 0
    changed: bool = False
    skipped: bool = False

    def update(self, props: dict[str, Any]) -> None:
        self.changed = False
        self.skipped = False
        raw = self.evaluate(props, self.active)
        if raw is None:
            self.skipped = True
            return
        raw = bool(raw)
        if self.active is None:
            self.active = raw
            self.pending = None
            self.pending_count = 0
            return
        if raw == self.active:
            self.pending = None
            self.pending_count = 0
            return
        if self.pending != raw:
            self.pending = raw
            self.pending_count = 1
        else:
            self.pending_count += 1
        if self.pending_count >= max(1, self.required_samples):
            self.active = raw
            self.switches += 1
            self.changed = True
            self.pending = None
            self.pending_count = 0

    def compact_state(self) -> str:
        if self.skipped:
            state = "?"
        elif self.active is None:
            state = "-"
        else:
            state = "1" if self.active else "0"
        marker = "!" if self.changed else " "
        return f"{state}{marker}{self.switches}"


def build_sequence(args: argparse.Namespace) -> list[Phase]:
    charge_on = Phase("CHARGE_ON", 1, args.power, 0, args.hold)
    charge_zero = Phase("ZERO_AFTER_CHARGE", 1, 0, 0, args.zero_hold)
    discharge_on = Phase("DISCHARGE_ON", 2, 0, args.power, args.hold)
    discharge_zero = Phase("ZERO_AFTER_DISCHARGE", 2, 0, 0, args.zero_hold)

    if args.mode == "charge":
        return [charge_on, charge_zero]
    if args.mode == "discharge":
        return [discharge_on, discharge_zero]
    if args.mode == "both":
        return [charge_on, charge_zero, discharge_on, discharge_zero]
    if args.mode == "direct-switch":
        return [charge_on, discharge_on, charge_on, charge_zero]
    raise ValueError(f"Unsupported mode: {args.mode}")


def make_detectors(args: argparse.Namespace) -> list[Detector]:
    required = max(1, args.required_samples)
    on_threshold = max(0, args.power_on_threshold)
    off_threshold = max(0, args.power_off_threshold)

    def power_raw(props: dict[str, Any], _previous: bool | None) -> bool:
        return measured_power(props) > 0

    def power_hyst(props: dict[str, Any], previous: bool | None) -> bool:
        power = measured_power(props)
        if previous:
            return power > off_threshold
        return power >= on_threshold

    def pack_state(props: dict[str, Any], _previous: bool | None) -> bool:
        return int_value(props.get("packState"), 0) in (1, 2)

    def pack_data(props: dict[str, Any], _previous: bool | None) -> bool:
        packs = props.get("_packData") or []
        return any(int_value(pack.get("state"), 0) in (1, 2) for pack in packs)

    def ac_status(props: dict[str, Any], _previous: bool | None) -> bool:
        return int_value(props.get("acStatus"), 0) in (1, 2)

    def command_echo(props: dict[str, Any], _previous: bool | None) -> bool:
        ac_mode = int_value(props.get("acMode"), 0)
        return (
            (ac_mode == 1 and int_value(props.get("inputLimit"), 0) > 0)
            or (ac_mode == 2 and int_value(props.get("outputLimit"), 0) > 0)
        )

    def composite(props: dict[str, Any], previous: bool | None) -> bool | None:
        data_ready = props.get("dataReady")
        if data_ready is not None and int_value(data_ready, 1) != 1:
            return None
        return (
            power_hyst(props, previous)
            or pack_state(props, previous)
            or pack_data(props, previous)
            or ac_status(props, previous)
        )

    return [
        Detector("power_raw", power_raw, required),
        Detector("power_hyst", power_hyst, required),
        Detector("pack_state", pack_state, required),
        Detector("pack_data", pack_data, required),
        Detector("ac_status", ac_status, required),
        Detector("cmd_echo", command_echo, required),
        Detector("composite", composite, required),
    ]


def sample_for(
    base_url: str,
    duration: float,
    interval: float,
    phase: str,
    detectors: list[Detector],
    args: argparse.Namespace,
    csv_writer: csv.DictWriter | None,
) -> None:
    started = time.monotonic()
    next_sample = started
    while True:
        now_mono = time.monotonic()
        if now_mono < next_sample:
            time.sleep(min(next_sample - now_mono, 0.2))
            continue
        report = get_report(base_url, args.timeout)
        props = dict(report.get("properties") or {})
        props["_packData"] = report.get("packData") or []
        for detector in detectors:
            detector.update(props)
        print_sample(phase, props, detectors)
        if csv_writer:
            csv_writer.writerow(sample_row(phase, props, detectors))
        if time.monotonic() - started >= duration:
            break
        next_sample += max(0.2, interval)


def print_sample(phase: str, props: dict[str, Any], detectors: list[Detector]) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    ac_mode = int_value(props.get("acMode"), 0)
    pack_state = int_value(props.get("packState"), -1)
    ac_status = int_value(props.get("acStatus"), -1)
    fields = (
        f"{stamp} {phase:<20} "
        f"acMode={ac_mode} "
        f"in={int_value(props.get('inputLimit'), 0):>4} "
        f"out={int_value(props.get('outputLimit'), 0):>4} "
        f"outPack={int_value(props.get('outputPackPower'), 0):>4} "
        f"packIn={int_value(props.get('packInputPower'), 0):>4} "
        f"packState={pack_state}:{PACK_STATE_LABELS.get(pack_state, 'unknown'):<11} "
        f"acStatus={ac_status}:{AC_STATUS_LABELS.get(ac_status, 'unknown'):<8} "
        f"dataReady={props.get('dataReady', '-')}"
    )
    detector_text = " ".join(
        f"{detector.name}={detector.compact_state()}" for detector in detectors
    )
    print(f"{fields} | {detector_text}", flush=True)


def sample_row(
    phase: str,
    props: dict[str, Any],
    detectors: list[Detector],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "phase": phase,
        "acMode": int_value(props.get("acMode"), 0),
        "inputLimit": int_value(props.get("inputLimit"), 0),
        "outputLimit": int_value(props.get("outputLimit"), 0),
        "outputPackPower": int_value(props.get("outputPackPower"), 0),
        "packInputPower": int_value(props.get("packInputPower"), 0),
        "packState": int_value(props.get("packState"), -1),
        "acStatus": int_value(props.get("acStatus"), -1),
        "dcStatus": int_value(props.get("dcStatus"), -1),
        "gridState": int_value(props.get("gridState"), -1),
        "dataReady": props.get("dataReady", ""),
    }
    for detector in detectors:
        row[f"{detector.name}_active"] = "" if detector.active is None else int(detector.active)
        row[f"{detector.name}_switches"] = detector.switches
        row[f"{detector.name}_changed"] = int(detector.changed)
        row[f"{detector.name}_skipped"] = int(detector.skipped)
    return row


def make_csv_writer(csv_file) -> csv.DictWriter:
    fieldnames = [
        "timestamp",
        "phase",
        "acMode",
        "inputLimit",
        "outputLimit",
        "outputPackPower",
        "packInputPower",
        "packState",
        "acStatus",
        "dcStatus",
        "gridState",
        "dataReady",
    ]
    for name in (
        "power_raw",
        "power_hyst",
        "pack_state",
        "pack_data",
        "ac_status",
        "cmd_echo",
        "composite",
    ):
        fieldnames.extend(
            [
                f"{name}_active",
                f"{name}_switches",
                f"{name}_changed",
                f"{name}_skipped",
            ]
        )
    writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
    writer.writeheader()
    return writer


def print_report_summary(report: dict[str, Any]) -> None:
    props = report.get("properties") or {}
    print(f"  sn: {report.get('sn', '')}")
    print(f"  product: {report.get('product', '')}")
    print(
        "  properties: "
        f"acMode={props.get('acMode')} "
        f"inputLimit={props.get('inputLimit')} "
        f"outputLimit={props.get('outputLimit')} "
        f"outputPackPower={props.get('outputPackPower')} "
        f"packInputPower={props.get('packInputPower')} "
        f"packState={props.get('packState')} "
        f"acStatus={props.get('acStatus')} "
        f"dataReady={props.get('dataReady')}"
    )


def confirm_run(args: argparse.Namespace) -> bool:
    print("Safety check:")
    print("- Stop Home Assistant/Gielz automations for this Zendure while probing.")
    print("- Use one physical Zendure only.")
    print("- Start with low --power, for example 50 or 100 W.")
    print("- The script sends smartMode=1 unless --no-smart-mode is used.")
    print("- Ctrl+C sends a final zero command unless --no-final-zero is used.")
    print("")
    answer = input("Type YES to send POST commands: ").strip()
    return answer == "YES"


def send_final_zero(
    base_url: str,
    sn: str,
    ac_mode: int,
    args: argparse.Namespace,
) -> None:
    ac_mode = ac_mode if ac_mode in (1, 2) else 1
    props = {
        "acMode": ac_mode,
        "inputLimit": 0,
        "outputLimit": 0,
    }
    if args.smart_mode:
        props["smartMode"] = 1
    try:
        post_properties(base_url, sn, props, args.timeout)
    except Exception as exc:
        print(f"Final zero command failed: {exc}", file=sys.stderr)


def get_report(base_url: str, timeout: float) -> dict[str, Any]:
    return http_json("GET", f"{base_url}/properties/report", None, timeout)


def post_properties(
    base_url: str,
    sn: str,
    props: dict[str, int],
    timeout: float,
) -> dict[str, Any]:
    return http_json(
        "POST",
        f"{base_url}/properties/write",
        {"sn": sn, "properties": props},
        timeout,
    )


def http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    timeout: float,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            if not text:
                return {}
            return json.loads(text)
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{method} {url} returned invalid JSON: {exc}") from exc


def normalize_base_url(value: str) -> str:
    value = value.strip()
    if not value.startswith(("http://", "https://")):
        value = f"http://{value}"
    parsed = parse.urlparse(value)
    path = parsed.path.rstrip("/")
    for suffix in ("/properties/report", "/properties/write"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    normalized = parsed._replace(path=path, params="", query="", fragment="")
    return parse.urlunparse(normalized).rstrip("/")


def measured_power(props: dict[str, Any]) -> int:
    return max(
        int_value(props.get("outputPackPower"), 0),
        int_value(props.get("packInputPower"), 0),
    )


def int_value(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    raise SystemExit(main())
