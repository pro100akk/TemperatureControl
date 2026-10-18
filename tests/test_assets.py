"""Check UI label completeness and valid packaged JSON."""

import json
from pathlib import Path
import unittest

import bootstrap  # noqa: F401
from custom_components.apartment_comfort.const import ACTUATOR_FIELDS, DEFAULTS, PERIODS, SENSOR_FIELDS

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/apartment_comfort"


class AssetTests(unittest.TestCase):
    def test_ui_labels_cover_configuration(self):
        expected = set(DEFAULTS) | set(SENSOR_FIELDS) | set(ACTUATOR_FIELDS)
        for language in ("en", "ru"):
            data = json.loads((COMPONENT / f"translations/{language}.json").read_text(encoding="utf-8"))
            for section in ("config", "options"):
                actual = set()
                for step in data[section]["step"].values():
                    actual |= set(step.get("data", {}))
                self.assertEqual(expected, actual, (language, section))

    def test_manifest_and_json_parse(self):
        for path in COMPONENT.rglob("*.json"):
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)
        manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["domain"], "apartment_comfort")
        self.assertTrue(manifest["config_flow"])

    def test_all_six_periods_in_period_step(self):
        data = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
        keys = set(data["config"]["step"]["periods"]["data"])
        self.assertEqual(keys, {f"{p}_{suffix}" for p in PERIODS for suffix in ("state", "temperature")})
