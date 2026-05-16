import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services.config_preset import ConfigPresetService


def test_config_preset_does_not_override_llm_injection_target():
    config = {
        "preset_and_basic": {
            "config_preset_mode": "balanced",
            "llm_injection_target": "user",
        }
    }

    merged = ConfigPresetService(config).apply()

    assert merged["llm_injection_target"] == "user"
