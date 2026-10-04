"""Complete v2 tool catalogue."""

from __future__ import annotations

from .adb import adb_tools
from .files import file_tools
from .registry import ToolRegistry
from .termux import build_termux_registry
from .termux_extra import extra_termux_tools


def build_full_registry(
    schedule_store=None, *, needs_authorisation=None, email_channel=None,
    document_store=None, search_settings=None,
) -> ToolRegistry:
    """Build the catalogue.

    Optional capabilities are only registered when their dependency is
    supplied: scheduling needs a store, email needs a configured channel.
    A caller that provides neither gets exactly the device-only catalogue, so
    tests, the doctor and one-shot scripts are unaffected.
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
    if email_channel is not None:
        from .email_tools import email_tools

        for tool in email_tools(email_channel):
            registry.register(tool)
    if document_store is not None:
        from .document_tools import document_tools

        for tool in document_tools(document_store):
            registry.register(tool)
    if search_settings is not None:
        from .search_tools import search_tools

        for tool in search_tools(search_settings):
            registry.register(tool)
    return registry
