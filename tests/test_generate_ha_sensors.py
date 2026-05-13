from __future__ import annotations

from tools.generate_ha_sensors import generate


def test_generator_preserves_legacy_ids_and_adds_fourth_device() -> None:
    output = generate(4, "EN")

    assert "unique_id: zendure_proxy_1_state_of_charge" in output
    assert "unique_id: zendure_proxy_4_state_of_charge" in output
    assert "value_json['properties']['electricLevel_4']" in output

