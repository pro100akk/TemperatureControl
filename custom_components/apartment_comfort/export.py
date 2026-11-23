"""Complete saved history bundles through HA's authenticated HTTP surface."""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
from secrets import token_urlsafe
from shutil import copyfile
from tempfile import TemporaryDirectory
from time import monotonic
from zipfile import ZipFile, ZIP_DEFLATED

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path

from .const import DOMAIN
from .diagnostics import async_get_config_entry_diagnostics
from .history import Anonymizer, records
from .version import VERSION

EXPORT_TTL = 600
MAX_TICKETS = 16


@contextmanager
def snapshot_history(folder, entry_id, lock):
    """Copy immutable input files; do not hold up recording while parsing ZIP data."""
    with TemporaryDirectory(prefix="apartment-comfort-export-") as snapshot:
        copies = []
        with lock:
            base = Path(folder) / f"{entry_id}.jsonl"
            paths = [p for p in base.parent.glob(f"{base.name}.*")
                     if p.name.removeprefix(f"{base.name}.").isdigit()]
            paths.sort(key=lambda p: int(p.suffix[1:]), reverse=True)
            if base.exists():
                paths.append(base)
            for index, path in enumerate(paths):
                destination = Path(snapshot) / str(index)
                copyfile(path, destination)
                copies.append(destination)
        yield copies


def build_bundle(folder, entry_id, lock, start, end, anonymize, diagnostics):
    """Stream all matching saved records from a disk snapshot into the archive."""
    stats = {"exported": 0, "invalid_lines": 0, "truncated": False}
    cleaner = Anonymizer() if anonymize else None
    buffer = BytesIO()
    with snapshot_history(folder, entry_id, lock) as paths, ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        with archive.open("history.jsonl", "w", force_zip64=True) as stream:
            for record in records(paths, start, end, stats):
                record = cleaner.record(record) if cleaner else record
                stream.write((json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
                stats["exported"] += 1
        report = cleaner.clean(diagnostics) if cleaner else diagnostics
        archive.writestr("diagnostics.json", json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        archive.writestr("manifest.json", json.dumps({
            "version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
            "start": start.isoformat(), "end": end.isoformat(), "anonymized": anonymize,
            "source_files": len(paths), **stats,
            "limits": {"records": None, "uncompressed_history_bytes": None},
            "diagnostics_scope": "current_state_at_download_not_historical",
        }, ensure_ascii=False, indent=2))
        archive.writestr("README.txt", (
            "Apartment Comfort\n"
            "history.jsonl: события выбранного периода, включая ошибки и команды.\n"
            "diagnostics.json: текущая диагностика на момент скачивания.\n"
            "manifest.json: период, число записей и повреждённые строки.\n"
            "Экспорт включает все корректные сохранённые события выбранного периода без ограничения количества и объёма.\n"
            "Удалённые ротацией и ранее не записанные события восстановить нельзя.\n"
            "exported=0: за этот период нет сохранённых событий; запись отладки должна быть включена заранее.\n"
            "Анонимизация удаляет свободный текст ошибок, но сохраняет коды и числовые данные.\n"
            "Времена и показания могут раскрывать распорядок дома.\n"
            "Отключённая анонимизация сохраняет исходные ошибки и идентификаторы; проверьте архив перед передачей.\n"
            "Экспорт не включает общие журналы HA и внутренних журналов других интеграций.\n"
        ))
    return buffer.getvalue()


class HistoryDownloads(HomeAssistantView):
    url = "/api/apartment_comfort/history/{ticket}"
    name = "api:apartment_comfort:history"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass
        self.tickets = {}
        self.busy = asyncio.Lock()

    def create_link(self, entry_id, start, end, anonymize):
        now = monotonic()
        self.tickets = {key: spec for key, spec in self.tickets.items()
                        if spec["expires"] > now and spec["entry_id"] != entry_id}
        while len(self.tickets) >= MAX_TICKETS:
            self.tickets.pop(next(iter(self.tickets)))
        ticket = token_urlsafe(32)
        self.tickets[ticket] = {"entry_id": entry_id, "start": start, "end": end,
                                "anonymize": anonymize, "expires": now + EXPORT_TTL}
        return async_sign_path(self.hass, self.url.format(ticket=ticket), timedelta(seconds=EXPORT_TTL))

    async def get(self, request, ticket):
        spec = self.tickets.get(ticket)
        if spec is None or spec["expires"] <= monotonic():
            self.tickets.pop(ticket, None)
            raise web.HTTPGone(text="Export link expired. Create a new export in integration settings.")
        entry = self.hass.config_entries.async_get_entry(spec["entry_id"])
        coordinator = getattr(entry, "runtime_data", None) if entry else None
        if entry is None or entry.domain != DOMAIN or coordinator is None or coordinator.stopping:
            raise web.HTTPNotFound(text="Integration is not loaded.")
        # Keep the single-export guard until executor work ends, even if the
        # browser disconnects and aiohttp cancels the request coroutine.
        body = await asyncio.shield(self.hass.async_create_task(self._async_bundle(entry, spec)))
        return web.Response(body=body, content_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="apartment-comfort-history.zip"',
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        })

    async def _async_bundle(self, entry, spec):
        if self.busy.locked():
            raise web.HTTPConflict(text="An export is already running. Try again shortly.")
        async with self.busy:
            recorder = entry.runtime_data.debug
            if recorder.task is not None:
                await asyncio.shield(recorder.task)
            report = await async_get_config_entry_diagnostics(self.hass, entry)
            try:
                return await self.hass.async_add_executor_job(
                    build_bundle, self.hass.config.path("apartment_comfort_debug"), entry.entry_id,
                    recorder.io_lock, spec["start"], spec["end"], spec["anonymize"], report)
            except (OSError, ValueError) as err:
                raise web.HTTPInternalServerError(text="Export failed. Check storage and try again.") from err


def setup_downloads(hass):
    data = hass.data.setdefault(DOMAIN, {})
    if "history_downloads" not in data:
        data["history_downloads"] = HistoryDownloads(hass)
        hass.http.register_view(data["history_downloads"])
    return data["history_downloads"]
