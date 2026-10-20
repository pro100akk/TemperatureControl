"""Read the release version from the integration's single source of truth."""

import json
from pathlib import Path

VERSION = json.loads(Path(__file__).with_name("manifest.json").read_text(encoding="utf-8"))["version"]
