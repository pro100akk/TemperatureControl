"""Load the actual integration modules without requiring a Home Assistant install."""

import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
for name, path in (
    ("custom_components", ROOT / "custom_components"),
    ("custom_components.apartment_comfort", ROOT / "custom_components/apartment_comfort"),
):
    package = ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules.setdefault(name, package)
