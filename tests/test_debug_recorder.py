"""Exercise actual files, asynchronous draining and recording failures."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bootstrap  # noqa: F401
from custom_components.apartment_comfort.const import DEFAULTS
from custom_components.apartment_comfort.debug_recorder import DebugRecorder, state_snapshot


class RecorderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = SimpleNamespace(
            config=SimpleNamespace(path=lambda folder: str(Path(self.temp.name) / folder)),
            async_create_task=asyncio.create_task,
            async_add_executor_job=asyncio.to_thread)

    def recorder(self, enabled=True):
        return DebugRecorder(self.hass, "zone", DEFAULTS | {"debug_enabled": enabled})

    async def test_disabled_never_creates_files_or_tasks(self):
        recorder = self.recorder(False)
        recorder.record("cycle", temperature=21)
        await recorder.async_close()
        self.assertIsNone(recorder.task)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    async def test_records_are_snapshots_and_close_flushes(self):
        recorder = self.recorder()
        payload = {"temperature": 21}
        recorder.record("cycle", inputs=payload, note="Ошибка")
        payload["temperature"] = 99
        await recorder.async_close()
        record = json.loads(recorder.path.read_text(encoding="utf-8"))
        self.assertEqual(record["data"]["inputs"]["temperature"], 21)
        self.assertEqual(record["data"]["note"], "Ошибка")
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(recorder.written, 1)
        recorder.record("ignored")
        self.assertEqual(recorder.written, 1)

    async def test_rotation_is_bounded_and_keeps_complete_json(self):
        recorder = self.recorder()
        recorder.max_bytes = 600
        recorder.backups = 2
        for number in range(20):
            recorder.record("cycle", number=number)
        await recorder.async_close()
        files = list(recorder.path.parent.iterdir())
        self.assertEqual(len(files), 3)
        for path in files:
            self.assertLessEqual(path.stat().st_size, 600)
            for line in path.read_text().splitlines():
                json.loads(line)
        last = json.loads(recorder.path.read_text().splitlines()[-1])
        self.assertEqual(last["data"]["number"], 19)

    async def test_queue_overflow_and_oversize_are_counted(self):
        recorder = self.recorder()
        recorder.MAX_PENDING = 2
        recorder.record("oversize", payload="x" * 65536)
        for number in range(5):
            recorder.record("cycle", number=number)
        await recorder.async_close()
        self.assertEqual(recorder.dropped, 4)
        self.assertEqual(recorder.written, 2)

    async def test_disk_failure_does_not_propagate_and_stops_retries(self):
        recorder = self.recorder()
        with patch.object(recorder, "_write", side_effect=OSError("disk full")) as write:
            with self.assertLogs("custom_components.apartment_comfort.debug_recorder", level="ERROR"):
                recorder.record("cycle")
                await recorder.task
            recorder.record("cycle")
            await recorder.async_close()
        self.assertEqual(write.call_count, 1)
        self.assertTrue(recorder.status["failed"])
        self.assertEqual(recorder.dropped, 1)

    def test_state_attributes_exclude_arbitrary_metadata(self):
        state = SimpleNamespace(state="heat", last_updated="now", attributes={
            "temperature": 22, "access_token": "secret", "friendly_name": "Bedroom"})
        self.assertEqual(state_snapshot(state)["attributes"], {"temperature": 22})

