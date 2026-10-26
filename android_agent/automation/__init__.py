"""Bounded, typed automation of other Android apps."""

from .backend import SUPPORTED_APPS, AdbBackend, Backend, FakeBackend, parse_hierarchy, screen
from .companion import CompanionBackend
from .models import (
    ERROR_CODES,
    AutomationError,
    UiElement,
    UiSnapshot,
    WorkflowResult,
    parse_bounds,
)
from .runtime import Limits, WorkflowRun
from .select import choose_backend

__all__ = [
    "ERROR_CODES",
    "SUPPORTED_APPS",
    "AdbBackend",
    "AutomationError",
    "Backend",
    "CompanionBackend",
    "FakeBackend",
    "Limits",
    "UiElement",
    "UiSnapshot",
    "WorkflowResult",
    "WorkflowRun",
    "choose_backend",
    "parse_bounds",
    "parse_hierarchy",
    "screen",
]
