"""Bounded, typed automation of other Android apps."""

from .backend import SUPPORTED_APPS, AdbBackend, Backend, FakeBackend, parse_hierarchy, screen
from .models import (
    ERROR_CODES,
    AutomationError,
    UiElement,
    UiSnapshot,
    WorkflowResult,
    parse_bounds,
)
from .runtime import Limits, WorkflowRun

__all__ = [
    "ERROR_CODES",
    "SUPPORTED_APPS",
    "AdbBackend",
    "AutomationError",
    "Backend",
    "FakeBackend",
    "Limits",
    "UiElement",
    "UiSnapshot",
    "WorkflowResult",
    "WorkflowRun",
    "parse_bounds",
    "parse_hierarchy",
    "screen",
]
