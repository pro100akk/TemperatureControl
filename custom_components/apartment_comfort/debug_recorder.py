"""Bounded, opt-in JSONL flight recorder; disk I/O runs outside the HA loop."""

from collections import deque
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from uuid import uuid4

from .version import VERSION

_LOGGER = logging.getLogger(__name__)
ATTRIBUTES = (
    "unit_of_measurement", "temperature", "current_temperature", "hvac_action",
    "hvac_modes", "min_temp", "max_temp", "target_temp_step", "percentage",
    "brightness", "supported_features", "supported_color_modes", "humidity",
)


def state_snapshot(state):
    """Only collect control-related attributes, never arbitrary device metadata."""
    if state is None:
        return None
    return {
        "state": state.state,
        "attributes": {k: state.attributes[k] for k in ATTRIBUTES if k in state.attributes},
        "last_updated": str(state.last_updated),
        "last_reported": str(getattr(state, "last_reported", state.last_updated)),
    }


class DebugRecorder:
    MAX_PENDING = 256
    MAX_RECORD_BYTES = 65536

    def __init__(self, hass, entry_id, config):
        self.hass = hass
        self.enabled = config["debug_enabled"]
        self.max_bytes = int(config["debug_max_file_mb"]) * 1024 * 1024
        self.backups = int(config["debug_backup_count"])
        self.path = (Path(hass.config.path("apartment_comfort_debug")) / f"{entry_id}.jsonl"
                     if self.enabled else None)
        self.session = uuid4().hex
        self.sequence = 0
        self.pending = deque()
        self.task = None
        self.closed = False
        self.failed = False
        self.dropped = 0
        self.written = 0

    @property
    def status(self):
        return {"enabled": self.enabled, "failed": self.failed, "closed": self.closed,
                "written": self.written, "dropped": self.dropped,
                "pending": len(self.pending)}

    def record(self, kind, **data):
        if not self.enabled or self.closed or self.failed:
            return
        self.sequence += 1
        try:
            line = (json.dumps({
                "schema_version": 1, "version": VERSION, "session": self.session,
                "sequence": self.sequence, "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": kind, "dropped_total": self.dropped, "data": data,
            }, ensure_ascii=False, default=str, allow_nan=False) + "\n").encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            self.dropped += 1
            return
        if len(line) > self.MAX_RECORD_BYTES or len(self.pending) >= self.MAX_PENDING:
            self.dropped += 1
            return
        self.pending.append(line)
        if self.task is None or self.task.done():
            self.task = self.hass.async_create_task(self._drain())

    async def _drain(self):
        try:
            while self.pending:
                batch = [self.pending.popleft() for _ in range(min(32, len(self.pending)))]
                await self.hass.async_add_executor_job(self._write, batch)
                self.written += len(batch)
        except Exception:  # Recording failures must never interrupt appliance control.
            self.failed = True
            self.dropped += len(batch) + len(self.pending)
            self.pending.clear()
            _LOGGER.exception("Debug recording stopped after a write error; toggle debug to retry")

    def _write(self, batch):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        size = self.path.stat().st_size if self.path.exists() else 0
        for line in batch:
            if size + len(line) > self.max_bytes:
                for index in range(self.backups, 0, -1):
                    source = self.path if index == 1 else Path(f"{self.path}.{index - 1}")
                    if source.exists():
                        source.replace(Path(f"{self.path}.{index}"))
                size = 0
            with self.path.open("ab") as stream:
                stream.write(line)
            size += len(line)

    async def async_close(self):
        self.closed = True
        if self.task is not None:
            await self.task
