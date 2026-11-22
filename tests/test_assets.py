"""Check UI label completeness and valid packaged JSON."""

import json
from html.parser import HTMLParser
from pathlib import Path
import unittest

import bootstrap  # noqa: F401
from custom_components.apartment_comfort.const import ACTUATOR_FIELDS, DEFAULTS, PERIODS, SENSOR_FIELDS

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/apartment_comfort"


class AssetTests(unittest.TestCase):
    def test_export_link_bypasses_ha_internal_navigation(self):
        class Anchors(HTMLParser):
            def __init__(self):
                super().__init__()
                self.anchors = []

            def handle_starttag(self, tag, attrs):
                if tag == "a":
                    self.anchors.append(dict(attrs))

        for name in ("strings.json", "translations/en.json", "translations/ru.json"):
            data = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
            description = data["options"]["step"]["export_ready"]["description"]
            parser = Anchors()
            self.assertNotIn("<a", description)
            parser.feed(description.format(
                download_open='<a href="/api/apartment_comfort/history/ticket?authSig=signed" target="_blank">',
                download_close="</a>"))
            self.assertEqual(len(parser.anchors), 1)
            anchor = parser.anchors[0]
            # HA's isNavigationClick deliberately ignores anchors with target.
            # target/href are also allowed by the frontend Markdown sanitizer.
            self.assertEqual(anchor["target"], "_blank")
            self.assertEqual(anchor["href"], "/api/apartment_comfort/history/ticket?authSig=signed")

    def test_ui_labels_cover_configuration(self):
        expected = set(DEFAULTS) | set(SENSOR_FIELDS) | set(ACTUATOR_FIELDS)
        for language in ("en", "ru"):
            data = json.loads((COMPONENT / f"translations/{language}.json").read_text(encoding="utf-8"))
            for section in ("config", "options"):
                actual = set()
                for key, step in data[section]["step"].items():
                    if key in ("export", "export_ready"):
                        continue
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
