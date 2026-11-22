"""Round-trip policy checkpoints, private history export and truncated files."""

from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import bootstrap  # noqa: F401
from custom_components.apartment_comfort.engine import Engine, Inputs
from custom_components.apartment_comfort.const import DEFAULTS
from custom_components.apartment_comfort.history import Anonymizer, export_history, parse_time, records
from custom_components.apartment_comfort.replay import checkpoint, replay_cycle


def recorded_cycle(engine, data, now, mode="heat"):
    saved = checkpoint(engine, now, 21.5)
    decision = engine.evaluate(data, now, mode, predicted_temperature=21.5)
    return json.loads(json.dumps({
        "schema_version": 1, "version": "0.7.0", "session": "private-session",
        "sequence": now, "timestamp": "2026-11-22T01:00:00+00:00", "event": "cycle",
        "data": {"replay": saved, "inputs": asdict(data), "decision": asdict(decision),
                 "mode": mode, "override": None}}))


class ReplayTests(unittest.TestCase):
    def test_frozen_pid_outage_and_sensor_failure_history(self):
        path = Path(__file__).parent / "fixtures/policy_history.jsonl"
        history = list(records([path]))
        self.assertEqual(len(history), 6)
        self.assertTrue(history[2]["data"]["decision"]["floor"])
        self.assertEqual(history[3]["data"]["decision"]["reason"], "temperature_unavailable")
        self.assertEqual(history[-1]["data"]["decision"]["status"], "off")
        anonymizer = Anonymizer()
        for record in history:
            self.assertEqual(replay_cycle(record["data"])["differences"], {})
            self.assertEqual(replay_cycle(anonymizer.record(record)["data"])["differences"], {})

    def test_cli_exports_selected_range_and_replays_export(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from tools.history import main
        path = Path(__file__).parent / "fixtures/policy_history.jsonl"
        with TemporaryDirectory() as folder, redirect_stdout(StringIO()) as output:
            destination = str(Path(folder) / "anonymous.jsonl")
            self.assertEqual(main(["export", str(path), "--start", "2026-11-22T11:01:00+10:00",
                                   "--end", "2026-11-22T11:07:00+10:00", "--output", destination]), 0)
            exported = list(records([destination]))
            self.assertEqual(len(exported), 3)
            self.assertEqual(main(["replay", destination]), 0)
            self.assertIn('"different": 0', output.getvalue())

    def test_stateful_pid_and_floor_timers_survive_json_roundtrip(self):
        for controller in ("hysteresis", "pi", "pid"):
            engine = Engine(DEFAULTS | {"controller": controller, "floor_entity": "climate.floor",
                                       "floor_enabled": True, "floor_delay_minutes": 5})
            results = []
            for now, temperature in ((10, 19), (70, 19.2), (400, 19.3), (450, None), (510, 24)):
                record = recorded_cycle(engine, Inputs(temperature=temperature, period="day", heating_available=False), now)
                replayed = replay_cycle(record["data"])
                self.assertEqual(replayed["differences"], {})
                results.append(replayed["decision"])
            self.assertTrue(results[2]["floor"])
            self.assertEqual(results[3]["status"], "blocked")
            self.assertFalse(results[4]["floor"])

    def test_policy_change_is_reported_without_actuation(self):
        record = recorded_cycle(Engine(DEFAULTS | {"radiator_entity": "climate.private"}),
                                Inputs(temperature=21, period="day"), 10)
        result = replay_cycle(record["data"], {"day_temperature": 20})
        self.assertIn("target", result["differences"])
        self.assertFalse(result["decision"]["radiator"])

    def test_anonymous_export_retains_replay_and_hides_identifiers_and_arbitrary_text(self):
        engine = Engine(DEFAULTS | {"radiator_entity": "climate.private_bedroom", "day_state": "Мой день",
                                   "name": "My home", "notification_service": "notify.my_phone"})
        record = recorded_cycle(engine, Inputs(temperature=20, period="Мой день"), 10)
        record["data"].update(error="http://192.168.1.7 token=secret", issues=["My home is unreachable"],
                              devices={"climate.private_bedroom": {"state": "heat"}},
                              unknown_field="someone@example.com")
        clean = Anonymizer().record(record)
        text = json.dumps(clean, ensure_ascii=False)
        for secret in ("private_bedroom", "Мой день", "My home", "notify.my_phone", "192.168", "secret", "someone@", "private-session"):
            self.assertNotIn(secret, text)
        self.assertEqual(replay_cycle(clean["data"])["differences"], {})

    def test_period_matching_hvac_mode_does_not_change_mode_or_dictionary_keys(self):
        engine = Engine(DEFAULTS | {"day_state": "heat", "morning_state": "state"})
        record = recorded_cycle(engine, Inputs(temperature=20, period="heat"), 10)
        clean = Anonymizer().record(record)
        self.assertEqual(clean["data"]["mode"], "heat")
        self.assertEqual(replay_cycle(clean["data"])["differences"], {})

    def test_range_includes_endpoints_offsets_and_counts_corruption(self):
        with TemporaryDirectory() as folder:
            source, target = Path(folder) / "log.jsonl", Path(folder) / "export.jsonl"
            record = recorded_cycle(Engine(DEFAULTS), Inputs(temperature=20, period="day"), 10)
            source.write_text(json.dumps(record) + '\n{"truncated":\n', encoding="utf-8")
            start = parse_time("2026-11-22T11:00:00+10:00")
            stats = export_history([source], target, start, start)
            self.assertEqual(stats, {"exported": 1, "invalid_lines": 1})
            self.assertEqual(len(list(records([target]))), 1)
            with self.assertRaises(FileExistsError):
                export_history([source], target, start, start)
            with self.assertRaises(ValueError):
                export_history([source], source, start, start)
            with self.assertRaises(ValueError):
                parse_time("2026-11-22T11:00:00")

