"""Complete ZIP contents, snapshot isolation and authenticated downloads."""

import asyncio
from datetime import datetime, timezone
import importlib
from io import BytesIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Lock
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zipfile import ZipFile

import bootstrap  # noqa: F401


def module(name, **attrs):
    result = ModuleType(name)
    result.__dict__.update(attrs)
    return result


class HTTPError(Exception):
    def __init__(self, *, text):
        super().__init__(text)


web = SimpleNamespace(Response=lambda **kw: SimpleNamespace(**kw),
                      HTTPGone=type("Gone", (HTTPError,), {}),
                      HTTPNotFound=type("NotFound", (HTTPError,), {}),
                      HTTPConflict=type("Conflict", (HTTPError,), {}),
                      HTTPInternalServerError=type("ServerError", (HTTPError,), {}))
with patch.dict(sys.modules, {
    "aiohttp": module("aiohttp", web=web),
    "homeassistant.components.http": module("homeassistant.components.http", HomeAssistantView=object),
    "homeassistant.components.http.auth": module("homeassistant.components.http.auth", async_sign_path=Mock(
        side_effect=lambda hass, path, expires: path + "?authSig=test")),
}):
    export = importlib.import_module("custom_components.apartment_comfort.export")

START = datetime(2026, 11, 22, 1, tzinfo=timezone.utc)
END = datetime(2026, 11, 22, 2, tzinfo=timezone.utc)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.row = {"timestamp": START.isoformat(), "event": "command_result", "session": "private",
                    "data": {"entity_id": "climate.bedroom", "success": False,
                             "error": "Private device at 192.168.1.5", "error_type": "ValueError"}}

    def build(self, anonymous=True):
        body = export.build_bundle(self.folder, "zone", Lock(), START, END, anonymous,
                                   {"issues": ["private current error"], "debug_recording": {"enabled": False}})
        with ZipFile(BytesIO(body)) as archive:
            self.assertEqual(set(archive.namelist()), {"history.jsonl", "diagnostics.json", "manifest.json", "README.txt"})
            return (archive.read("history.jsonl").decode(), json.loads(archive.read("manifest.json")),
                    json.loads(archive.read("diagnostics.json")))

    def test_anonymous_and_raw_exports_include_rotations_but_not_other_zones(self):
        (self.folder / "zone.jsonl.1").write_text(json.dumps(self.row) + "\n", encoding="utf-8")
        (self.folder / "zone.jsonl").write_text(json.dumps(self.row) + '\n{"bad":\n', encoding="utf-8")
        (self.folder / "other.jsonl").write_text(json.dumps(self.row) + "\n", encoding="utf-8")
        text, manifest, report = self.build()
        self.assertEqual(manifest["exported"], 2)
        self.assertEqual(manifest["source_files"], 2)
        self.assertEqual(manifest["invalid_lines"], 1)
        self.assertNotIn("192.168", text)
        self.assertNotIn("bedroom", text)
        self.assertEqual(report["issues"], "[redacted]")
        text, manifest, report = self.build(False)
        self.assertIn("Private device at 192.168.1.5", text)
        self.assertFalse(manifest["anonymized"])
        self.assertEqual(report["issues"], ["private current error"])

    def test_empty_range_and_unlimited_export_are_explicit(self):
        _, manifest, _ = self.build()
        self.assertEqual(manifest["exported"], 0)
        self.assertFalse(manifest["truncated"])
        self.assertEqual(manifest["limits"], {"records": None, "uncompressed_history_bytes": None})

    def test_all_events_beyond_previous_count_and_byte_limits_in_both_modes(self):
        count = 10002
        for name, begin, end in (("zone.jsonl.1", 0, 5001), ("zone.jsonl", 5001, count)):
            with (self.folder / name).open("w", encoding="utf-8") as stream:
                for sequence in range(begin, end):
                    row = dict(self.row, sequence=sequence,
                               timestamp=(END if sequence == count - 1 else START).isoformat(),
                               data=self.row["data"] | {"detail": "x" * 2000})
                    stream.write(json.dumps(row) + "\n")
        self.assertGreater(sum(p.stat().st_size for p in self.folder.iterdir()), 16 * 1024 * 1024)
        for anonymous in (False, True):
            with self.subTest(anonymous=anonymous):
                text, manifest, _ = self.build(anonymous)
                rows = [json.loads(line) for line in text.splitlines()]
                self.assertEqual([row["sequence"] for row in rows], list(range(count)))
                self.assertEqual(rows[-1]["timestamp"], END.isoformat())
                self.assertEqual(manifest["exported"], count)
                self.assertFalse(manifest["truncated"])
                self.assertEqual(manifest["source_files"], 2)

    def test_snapshot_releases_writer_lock_and_survives_source_rotation(self):
        original = self.folder / "zone.jsonl"
        original.write_text(json.dumps(self.row) + "\n", encoding="utf-8")
        lock = Lock()
        saved_paths = []
        real_records = export.records
        def read_snapshot(paths, *args):
            self.assertFalse(lock.locked())
            saved_paths.extend(paths)
            original.unlink()
            return real_records(paths, *args)
        with patch.object(export, "records", side_effect=read_snapshot):
            body = export.build_bundle(self.folder, "zone", lock, START, END, False, {})
        with ZipFile(BytesIO(body)) as archive:
            self.assertEqual(json.loads(archive.read("history.jsonl")), self.row)
        self.assertTrue(saved_paths)
        self.assertTrue(all(not path.exists() for path in saved_paths))

    def test_failed_snapshot_cleans_temporary_files_and_preserves_source(self):
        original = self.folder / "zone.jsonl"
        content = json.dumps(self.row) + "\n"
        original.write_text(content, encoding="utf-8")
        saved_paths = []
        def failed_copy(source, destination):
            saved_paths.append(destination)
            destination.write_text("partial", encoding="utf-8")
            raise OSError("disk full")
        with patch.object(export, "copyfile", side_effect=failed_copy):
            with self.assertRaises(OSError):
                self.build()
        self.assertTrue(all(not path.exists() for path in saved_paths))
        self.assertEqual(original.read_text(encoding="utf-8"), content)

    def test_range_excludes_old_events(self):
        old = dict(self.row, timestamp="2026-11-21T01:00:00+00:00")
        (self.folder / "zone.jsonl").write_text(json.dumps(old) + "\n", encoding="utf-8")
        self.assertEqual(self.build()[1]["exported"], 0)


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entry = SimpleNamespace(entry_id="zone", domain="apartment_comfort", runtime_data=SimpleNamespace(
            stopping=False, debug=SimpleNamespace(task=None, io_lock=Lock())))
        self.hass = SimpleNamespace(data={}, config=SimpleNamespace(path=lambda folder: folder),
            async_create_task=asyncio.create_task,
            config_entries=SimpleNamespace(async_get_entry=lambda entry_id: self.entry),
            http=SimpleNamespace(register_view=Mock()), async_add_executor_job=AsyncMock(return_value=b"zip"))
        self.downloads = export.setup_downloads(self.hass)

    def ticket(self):
        link = self.downloads.create_link("zone", START, END, True)
        return link.split("/")[-1].split("?")[0]

    async def test_link_is_signed_private_bounded_and_view_registered_once(self):
        self.assertIs(export.setup_downloads(self.hass), self.downloads)
        self.hass.http.register_view.assert_called_once()
        self.assertTrue(self.downloads.requires_auth)
        old = self.ticket()
        new = self.ticket()
        self.assertNotEqual(old, new)
        self.assertNotIn(old, self.downloads.tickets)
        with self.assertRaises(web.HTTPGone):
            await self.downloads.get(None, old)
        with patch.object(export, "async_get_config_entry_diagnostics", AsyncMock(return_value={})):
            response = await self.downloads.get(None, new)
        self.assertEqual(response.body, b"zip")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("attachment", response.headers["Content-Disposition"])

    async def test_expired_unloaded_and_concurrent_downloads_are_rejected(self):
        ticket = self.ticket()
        self.downloads.tickets[ticket]["expires"] = 0
        with self.assertRaises(web.HTTPGone):
            await self.downloads.get(None, ticket)
        ticket = self.ticket()
        self.entry.runtime_data.stopping = True
        with self.assertRaises(web.HTTPNotFound):
            await self.downloads.get(None, ticket)
        self.entry.runtime_data.stopping = False
        async with self.downloads.busy:
            with self.assertRaises(web.HTTPConflict):
                await self.downloads.get(None, ticket)

    async def test_storage_errors_do_not_leak_paths_and_release_busy_lock(self):
        self.hass.async_add_executor_job.side_effect = OSError("secret path")
        with patch.object(export, "async_get_config_entry_diagnostics", AsyncMock(return_value={})):
            with self.assertRaises(web.HTTPInternalServerError) as result:
                await self.downloads.get(None, self.ticket())
        self.assertNotIn("secret", str(result.exception))
        self.assertFalse(self.downloads.busy.locked())

    async def test_disconnected_browser_does_not_allow_parallel_executor_exports(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def slow_build(*args):
            started.set()
            await finish.wait()
            return b"zip"

        self.hass.async_add_executor_job.side_effect = slow_build
        with patch.object(export, "async_get_config_entry_diagnostics", AsyncMock(return_value={})):
            task = asyncio.create_task(self.downloads.get(None, self.ticket()))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(self.downloads.busy.locked())
            with self.assertRaises(web.HTTPConflict):
                await self.downloads.get(None, self.ticket())
            finish.set()
            await asyncio.sleep(0)
            self.assertFalse(self.downloads.busy.locked())
