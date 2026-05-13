#!/usr/bin/env python3
from __future__ import annotations

import argparse


LABELS = {
    "EN": {
        "soc": "Zendure {n} State of Charge",
        "power_command": "Zendure {n} Power Command",
        "power": "Zendure {n} Power",
        "stale": "Zendure {n} Proxy Data Stale",
        "serial": "Zendure {n} Serial Number",
        "ip": "Zendure {n} IP Address",
        "yes": "Yes",
        "no": "No",
    },
    "NL": {
        "soc": "Zendure {n} Laadpercentage",
        "power_command": "Zendure {n} Vermogen Opdracht",
        "power": "Zendure {n} Vermogen",
        "stale": "Zendure {n} Proxy Data Verouderd",
        "serial": "Zendure {n} Serienummer",
        "ip": "Zendure {n} IP-adres",
        "yes": "Ja",
        "no": "Nee",
    },
}


def unique_id(n: int, suffix: str) -> str:
    if n <= 3:
        legacy = {
            "state_of_charge": f"zendure_proxy_{n}_state_of_charge",
            "latest_power_command": f"zendure_proxy_{n}_latest_power_command",
            "power": f"zendure_proxy_{n}_power",
            "serial": f"zendure_proxy_{n}_serial",
            "ip_address": f"Zendure_proxy_ip_address_{n}",
        }
        if suffix in legacy:
            return legacy[suffix]
    return f"zendure_proxy_{n}_{suffix}"


def generate(device_count: int, language: str) -> str:
    labels = LABELS[language]
    lines = ["####### ZENDURE PYTHON PROXY SENSORS #######", ""]
    for n in range(1, device_count + 1):
        lines.extend(
            [
                f'      - name: "{labels["soc"].format(n=n)}"',
                f"        value_template: \"{{{{ value_json['properties']['electricLevel_{n}'] }}}}\"",
                "        device_class: battery",
                '        unit_of_measurement: "%"',
                "        state_class: measurement",
                f"        unique_id: {unique_id(n, 'state_of_charge')}",
                "",
                f'      - name: "{labels["power_command"].format(n=n)}"',
                f"        value_template: \"{{{{ value_json['properties']['latestPowerCmd_{n}'] | int }}}}\"",
                '        unit_of_measurement: "W"',
                "        state_class: measurement",
                "        device_class: power",
                f"        unique_id: {unique_id(n, 'latest_power_command')}",
                "",
                f'      - name: "{labels["power"].format(n=n)}"',
                "        value_template: >",
                f"          {{% set charging = value_json['properties']['gridInputPower_{n}'] | int %}}",
                f"          {{% set discharging = - (value_json['properties']['outputHomePower_{n}'] | int) %}}",
                "          {% if charging != 0 %}",
                "            {{ charging }}",
                "          {% else %}",
                "            {{ discharging }}",
                "          {% endif %}",
                '        unit_of_measurement: "W"',
                "        state_class: measurement",
                "        device_class: power",
                f"        unique_id: {unique_id(n, 'power')}",
                "",
                f'      - name: "{labels["stale"].format(n=n)}"',
                "        value_template: >",
                f"          {{% set stale = value_json['properties']['proxyDeviceStale_{n}'] | int %}}",
                f"          {{{{ '{labels['yes']}' if stale == 1 else '{labels['no']}' }}}}",
                "        icon: mdi:cached",
                f"        unique_id: {unique_id(n, 'proxy_data_stale')}",
                "",
                f'      - name: "{labels["serial"].format(n=n)}"',
                f'        value_template: "{{{{ value_json.sn_{n} }}}}"',
                "        icon: mdi:identifier",
                f"        unique_id: {unique_id(n, 'serial')}",
                "",
                f'      - name: "{labels["ip"].format(n=n)}"',
                f"        value_template: \"{{{{ value_json.get('properties', {{}}).get('ipAddress_{n}', 'unknown') }}}}\"",
                "        icon: mdi:ip",
                f"        unique_id: {unique_id(n, 'ip_address')}",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--devices", type=int, required=True)
    parser.add_argument("--language", choices=["EN", "NL"], default="EN")
    args = parser.parse_args()
    print(generate(args.devices, args.language))


if __name__ == "__main__":
    main()

