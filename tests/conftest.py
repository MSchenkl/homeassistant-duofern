"""Load DuoFern modules without importing Home Assistant for unit tests."""

from __future__ import annotations

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
CUSTOM_COMPONENTS = REPO_ROOT / "custom_components"
DUOFERN_PACKAGE = CUSTOM_COMPONENTS / "duofern"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(CUSTOM_COMPONENTS)]
sys.modules.setdefault("custom_components", custom_components)

duofern = types.ModuleType("custom_components.duofern")
duofern.__path__ = [str(DUOFERN_PACKAGE)]
sys.modules.setdefault("custom_components.duofern", duofern)
