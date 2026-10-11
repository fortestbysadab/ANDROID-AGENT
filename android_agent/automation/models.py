"""Typed models for app automation.

Element references are deliberately **snapshot-scoped**. An accessibility
node id is not a durable handle: the moment the screen changes it may point
at something else, or nothing. So every element is addressed as
(snapshot_id, element_id), and acting on a reference from a superseded
snapshot is refused rather than retried against whatever is there now.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

#: Stable error codes. The model sees these, so they must describe the
#: situation rather than the implementation.
ERROR_CODES = (
    "backend_unavailable",      # no ADB, no companion
    "app_not_installed",
    "target_not_found",
    "target_ambiguous",         # more than one element matched
    "stale_snapshot",           # the screen moved on
    "state_timeout",            # the expected screen never appeared
    "login_required",
    "challenge_required",       # CAPTCHA, OTP, "confirm it's you"
    "unsupported_screen",
    "workflow_budget_exhausted",
    "unknown_workflow",
    "invalid_parameters",
    "verification_failed",      # the action ran but the screen disagrees
)

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


class AutomationError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool = False):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown automation error code: {code}")
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class UiElement:
    """One visible, addressable thing on screen."""

    element_id: str
    text: str
    description: str
    resource_id: str
    class_name: str
    clickable: bool
    editable: bool
    enabled: bool
    #: left, top, right, bottom in device pixels.
    bounds: tuple[int, int, int, int]

    @property
    def centre(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2

    @property
    def label(self) -> str:
        """What a human would call this element."""
        return self.text or self.description or self.resource_id.split("/")[-1]

    def on_screen(self, width: int, height: int) -> bool:
        left, top, right, bottom = self.bounds
        return right > left and bottom > top and left < width and top < height

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.element_id,
            "label": self.label,
            "text": self.text,
            "description": self.description,
            "resource_id": self.resource_id,
            "clickable": self.clickable,
            "editable": self.editable,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class UiSnapshot:
    """One reading of the screen. References are only valid against this."""

    snapshot_id: str
    package: str
    elements: tuple[UiElement, ...]
    width: int = 0
    height: int = 0
    truncated: bool = False

    @classmethod
    def new_id(cls) -> str:
        return uuid.uuid4().hex[:12]

    def get(self, element_id: str) -> UiElement | None:
        for element in self.elements:
            if element.element_id == element_id:
                return element
        return None

    def find(
        self, *, text: str = "", resource_id: str = "", editable: bool | None = None,
        clickable: bool | None = None, exact: bool = False,
    ) -> list[UiElement]:
        """Match on meaning, not position.

        `resource_id` is matched first and preferred by callers because it
        survives a language change; matching visible text would break the
        moment the phone is switched to Hindi.
        """
        needle = text.casefold().strip()
        found = []
        for element in self.elements:
            if resource_id and not element.resource_id.endswith(resource_id):
                continue
            if editable is not None and element.editable != editable:
                continue
            if clickable is not None and element.clickable != clickable:
                continue
            if needle:
                haystack = f"{element.text} {element.description}".casefold()
                if exact:
                    if needle not in (
                        element.text.casefold().strip(),
                        element.description.casefold().strip(),
                    ):
                        continue
                elif needle not in haystack:
                    continue
            found.append(element)
        return found

    def visible_text(self, limit: int = 2000) -> str:
        parts = [element.label for element in self.elements if element.label.strip()]
        joined = " · ".join(dict.fromkeys(parts))
        return joined[:limit]

    def as_dict(self, limit: int = 60) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot_id,
            "package": self.package,
            "truncated": self.truncated,
            "elements": [element.as_dict() for element in self.elements[:limit]],
        }


@dataclass
class WorkflowResult:
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    code: str | None = None
    steps: list[str] = field(default_factory=list)


def parse_bounds(raw: str) -> tuple[int, int, int, int]:
    match = _BOUNDS.search(raw or "")
    if not match:
        return (0, 0, 0, 0)
    left, top, right, bottom = (int(value) for value in match.groups())
    return (left, top, right, bottom)
