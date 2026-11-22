"""Export or replay Apartment Comfort JSONL on any computer with Python 3.12+."""

import argparse
import glob
import json
from pathlib import Path
import sys
from types import ModuleType

# Load only the pure Python modules; the package initializer requires live HA.
ROOT = Path(__file__).resolve().parents[1]
for name, path in (("custom_components", ROOT / "custom_components"),
                   ("custom_components.apartment_comfort", ROOT / "custom_components/apartment_comfort")):
    package = ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules.setdefault(name, package)

from custom_components.apartment_comfort.history import export_history, parse_time, records
from custom_components.apartment_comfort.replay import replay_cycle


def rotation_order(path):
    """Read oldest rotated file first, then the current JSONL for each zone."""
    base, separator, suffix = path.name.partition(".jsonl")
    rotation = int(suffix[1:]) if suffix.startswith(".") and suffix[1:].isdigit() else 0
    return str(path.parent), base if separator else path.name, -rotation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Export anonymized history for an inclusive time range")
    replay = commands.add_parser("replay", help="Compare recorded policy decisions with current code")
    for command in (export, replay):
        command.add_argument("logs", nargs="+", help="Files or quoted wildcard patterns (one zone)")
    export.add_argument("--start", required=True, type=parse_time)
    export.add_argument("--end", required=True, type=parse_time)
    export.add_argument("--output", required=True)
    replay.add_argument("--overrides", help="JSON object of experimental configuration overrides")
    args = parser.parse_args(argv)
    paths = sorted({Path(path).resolve() for pattern in args.logs for path in glob.glob(pattern)},
                   key=rotation_order)
    if not paths:
        parser.error("No input files matched")
    try:
        if args.command == "export":
            result = export_history(paths, args.output, args.start, args.end)
            print(json.dumps(result, indent=2))
            return 0
        overrides = json.loads(args.overrides) if args.overrides else None
        if overrides is not None and not isinstance(overrides, dict):
            raise ValueError("Overrides must be a JSON object")
        stats = {"checked": 0, "different": 0, "unsupported": 0, "invalid_lines": 0}
        for record in records(paths, stats=stats):
            if record["event"] != "cycle":
                continue
            data = record["data"]
            if not isinstance(data.get("replay"), dict) or data["replay"].get("version") != 1:
                stats["unsupported"] += 1
                continue
            try:
                result = replay_cycle(data, overrides)
            except (KeyError, TypeError, ValueError):
                stats["unsupported"] += 1
                continue
            stats["checked"] += 1
            if result["differences"]:
                stats["different"] += 1
                print(json.dumps({"session": record.get("session"), "sequence": record.get("sequence"),
                                  "differences": result["differences"]}, ensure_ascii=False))
        print(json.dumps(stats, indent=2))
        return int(bool(stats["different"] or stats["unsupported"] or stats["invalid_lines"] or not stats["checked"]))
    except (OSError, ValueError) as err:
        parser.error(str(err))


if __name__ == "__main__":
    raise SystemExit(main())
