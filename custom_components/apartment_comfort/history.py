"""Offline streaming history selection and conservative pseudonymized export."""

import ast
from dataclasses import fields
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .const import DEFAULTS, SENSOR_FIELDS, ACTUATOR_FIELDS
from .engine import Inputs, Decision


def parse_time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Specify a timezone, for example +10:00 or Z")
    return result.astimezone(timezone.utc)


def records(paths, start=None, end=None, stats=None):
    """Yield complete records; count truncated/corrupt lines rather than hiding them."""
    if start and end and start > end:
        raise ValueError("Start must not be after end")
    stats = stats if stats is not None else {}
    for path in paths:
        with Path(path).open("rb") as stream:
            while line := stream.readline(1024 * 1024 + 1):
                if len(line) > 1024 * 1024:
                    while line and not line.endswith(b"\n"):
                        line = stream.readline(1024 * 1024 + 1)
                    stats["invalid_lines"] = stats.get("invalid_lines", 0) + 1
                    continue
                try:
                    record = json.loads(line)
                    stamp = parse_time(record["timestamp"])
                    if not isinstance(record["data"], dict) or not isinstance(record["event"], str):
                        raise ValueError("Invalid event")
                except (ValueError, KeyError, TypeError, UnicodeDecodeError, AttributeError):
                    stats["invalid_lines"] = stats.get("invalid_lines", 0) + 1
                    continue
                if (start and stamp < start) or (end and stamp > end):
                    continue
                yield record


class Anonymizer:
    """Keep numeric evidence and known code literals; alias all other text/keys.

    Aliases are per export, with no reverse mapping in the output. This deliberately
    removes free-text exception messages, including URLs, IPs and tokens.
    """

    def __init__(self):
        self.aliases = {}
        self.periods = {}
        self.safe = set(DEFAULTS) | set(SENSOR_FIELDS) | set(ACTUATOR_FIELDS)
        self.safe.update(field.name for cls in (Inputs, Decision) for field in fields(cls))
        # Code literals form the allowlist, not arbitrary strings from the journal.
        for path in Path(__file__).parent.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if re.fullmatch(r"[a-z_][a-z_0-9]*", node.value):
                        self.safe.add(node.value)
        self.safe.update(("°C", "°F", "%", "ppm", "W", "kW", "HomeAssistantError",
                          "ValueError", "TimeoutError", "OSError"))

    def alias(self, value, prefix="text"):
        return self.aliases.setdefault(value, f"{prefix}_{len(self.aliases) + 1}")

    def clean(self, value, key=""):
        if key in ("name", "notification_service", "error", "issues"):
            return "[redacted]"
        if isinstance(value, dict):
            return {k if k in self.safe else self.clean(k): self.clean(v, k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(item, key) for item in value]
        if not isinstance(value, str):
            return value
        if key == "session":
            return self.alias(value, "session")
        if key in ("period",) or key.endswith("_state"):
            return self.periods.setdefault(value, f"period_{len(self.periods) + 1}")
        if key in ("timestamp", "last_updated", "last_reported"):
            try:
                return parse_time(value).isoformat()
            except ValueError:
                return "[redacted]"
        if value in self.safe or re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", value):
            return value
        if key == "version" and re.fullmatch(r"\d+\.\d+\.\d+", value):
            return value
        if key == "kitchen_speed_curve" and re.fullmatch(r"[\d.,: ]+", value):
            return value
        return self.alias(value)

    def record(self, record):
        # Seed period aliases before processing inputs so custom state names match.
        data = record["data"]
        config = data.get("replay", {}).get("config", data.get("config", {}))
        for key, value in config.items():
            if key.endswith("_state") and isinstance(value, str):
                self.periods.setdefault(value, f"period_{len(self.periods) + 1}")
        result = self.clean(record)
        result["anonymized"] = True
        return result


def export_history(paths, output, start, end):
    output = Path(output)
    if any(Path(path).resolve() == output.resolve() for path in paths):
        raise ValueError("Output must not replace an input log")
    if start > end:
        raise ValueError("Start must not be after end")
    stats = {"exported": 0, "invalid_lines": 0}
    anonymizer = Anonymizer()
    # Exclusive creation prevents accidental overwrite, including the original logs.
    with output.open("x", encoding="utf-8") as stream:
        for record in records(paths, start, end, stats):
            stream.write(json.dumps(anonymizer.record(record), ensure_ascii=False, allow_nan=False) + "\n")
            stats["exported"] += 1
    return stats
