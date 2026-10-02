"""Prepare a local Gielz package; never deploy or overwrite the input file.

Requires PyYAML. Only the rest: section is serialized again. All other sections
retain their original bytes, including the Gielz automation and templates.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import re

import yaml


TEMPERATURE_IDS = {
    "zendure_2400_ac_omvormer_temperatuur",
    *(f"zendure_2400_ac_batterij_{n}_temperatuur" for n in range(1, 7)),
}
AVAILABILITY = "{{ is_state('sensor.proxy_zendure_pool_healthy', 'Healthy') }}"


def temperature_availability(entity_id: str) -> str:
    pack_match = re.fullmatch(r"zendure_2400_ac_batterij_(\d+)_temperatuur", entity_id)
    if pack_match:
        index = int(pack_match.group(1)) - 1
        return "\n".join([
            "{% set order = states('input_text.zendure_2400_ac_batterij_volgorde') %}",
            "{% set ordered = order.split(';') if order not in ['unknown', 'unavailable', ''] else [] %}",
            f"{{% set index = (ordered[{index}] | int(0)) - 1 if ordered | length > {index} else {index} %}}",
            "{% set slots = value_json.get('packDeviceSlots', []) %}",
            "{% if 0 <= index < slots | length %}",
            "  {{ is_state('sensor.zendure_' ~ slots[index] ~ '_health', 'Healthy') }}",
            "{% else %}",
            "  {{ is_state('sensor.proxy_zendure_pool_healthy', 'Healthy') and value_json.get('packData', []) | length > index and index >= 0 }}",
            "{% endif %}",
        ])
    return "\n".join([
        "{% set slots = value_json.get('inverterTemperatureDeviceSlots', []) %}",
        "{% if slots %}",
        "  {% set valid = namespace(healthy=true) %}",
        "  {% for slot in slots %}",
        "    {% if not is_state('sensor.zendure_' ~ slot ~ '_health', 'Healthy') %}",
        "      {% set valid.healthy = false %}",
        "    {% endif %}",
        "  {% endfor %}",
        "  {{ valid.healthy }}",
        "{% else %}",
        "  {{ is_state('sensor.proxy_zendure_pool_healthy', 'Healthy') }}",
        "{% endif %}",
    ])


def split_temperature_group(groups: list[dict]) -> list[dict]:
    result = deepcopy(groups)
    matches = [group for group in result if any(
        str(sensor.get("unique_id", "")).lower() in TEMPERATURE_IDS
        for sensor in group.get("sensor", [])
    )]
    if len(matches) != 1:
        raise ValueError("Expected all seven Gielz temperatures in one REST group")
    group = matches[0]
    temperatures = [sensor for sensor in group["sensor"] if
                    str(sensor.get("unique_id", "")).lower() in TEMPERATURE_IDS]
    ids = [str(sensor["unique_id"]).lower() for sensor in temperatures]
    if len(ids) != 7 or set(ids) != TEMPERATURE_IDS:
        raise ValueError("Missing or duplicate Gielz temperature unique_id")
    if group.get("scan_interval") != 1:
        raise ValueError("Expected the original fast Gielz REST interval of 1 second")
    slow = {key: deepcopy(value) for key, value in group.items()
            if key not in {"sensor", "binary_sensor"}}
    slow.update(scan_interval=600, sensor=temperatures)
    group["sensor"] = [sensor for sensor in group["sensor"] if sensor not in temperatures]
    for sensor in temperatures:
        existing = sensor.get("availability")
        if existing and existing != AVAILABILITY:
            raise ValueError("Existing temperature availability needs manual review")
        sensor["availability"] = temperature_availability(str(sensor["unique_id"]).lower())
    result.insert(result.index(group) + 1, slow)
    return result


def prepare_package(source: str) -> str:
    # BaseLoader permits unrelated HA tags such as !include and !secret.
    root = yaml.compose(source, Loader=yaml.BaseLoader)
    if not isinstance(root, yaml.MappingNode):
        raise ValueError("Expected a Home Assistant YAML package")
    rest = [(key, value) for key, value in root.value if key.value == "rest"]
    if len(rest) != 1:
        raise ValueError("Expected exactly one rest: section")
    key, value = rest[0]
    section = source[key.start_mark.index:value.end_mark.index]
    parsed = yaml.safe_load(section)
    transformed = {"rest": split_temperature_group(parsed["rest"])}
    replacement = yaml.safe_dump(transformed, sort_keys=False, allow_unicode=True, width=1000)
    output = source[:key.start_mark.index] + replacement + source[value.end_mark.index:]
    # Verify the generated section and preserve every other top-level node.
    yaml.compose(output, Loader=yaml.BaseLoader)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = prepare_package(args.input.read_text(encoding="utf-8"))
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(output)
    print(f"Prepared {args.output}; input unchanged. No Home Assistant deployment performed.")


if __name__ == "__main__":
    main()
