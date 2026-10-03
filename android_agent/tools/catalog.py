"""Complete v2 tool catalogue."""

from __future__ import annotations

from .adb import adb_tools
from .files import file_tools
from .registry import ToolRegistry
from .termux import build_termux_registry
from .termux_extra import extra_termux_tools


def build_full_registry(schedule_store=None, *, needs_authorisation=None) -> ToolRegistry:
    """Build the catalogue.

    The scheduling tools need a store to write to, so they are only added when
    one is supplied. Callers without scheduling - tests, the doctor, one-shot
    scripts - get exactly the previous catalogue.
    """
    registry = build_termux_registry()
    for tool in [*file_tools(), *adb_tools(), *extra_termux_tools()]:
        registry.register(tool)
    if schedule_store is not None:
        from .schedule_tools import schedule_tools

        if needs_authorisation is None:
            raise ValueError("scheduling requires a needs_authorisation callback")
        for tool in schedule_tools(
            schedule_store, registry, needs_authorisation=needs_authorisation
        ):
            registry.register(tool)
    return registry
