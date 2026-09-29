"""Complete v2 tool catalogue."""

from __future__ import annotations

from .adb import adb_tools
from .files import file_tools
from .registry import ToolRegistry
from .termux import build_termux_registry
from .termux_extra import extra_termux_tools


def build_full_registry() -> ToolRegistry:
    registry = build_termux_registry()
    for tool in [*file_tools(), *adb_tools(), *extra_termux_tools()]:
        registry.register(tool)
    return registry
