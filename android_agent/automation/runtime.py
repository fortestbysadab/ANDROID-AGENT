"""Bounded workflow execution.

Every workflow runs against a deadline and an action budget, and every step
that changes the screen is followed by a check that the screen actually
changed as expected. A tap that "succeeded" is not evidence of anything:
the dispatcher accepted a touch event, which is a different claim from the
app having done what was asked.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .models import AutomationError, UiElement, UiSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limits:
    """Conservative ceilings. A workflow that needs more is a design error."""

    seconds: float = 45.0
    actions: int = 20
    retries: int = 2
    text_chars: int = 500
    settle_seconds: float = 0.9
    state_timeout: float = 10.0


@dataclass
class WorkflowRun:
    """One execution: holds the budget, the current screen and the trail."""

    backend: object
    limits: Limits = field(default_factory=Limits)
    steps: list[str] = field(default_factory=list)
    actions_used: int = 0
    snapshot: UiSnapshot | None = None
    _deadline: float = 0.0

    def __post_init__(self) -> None:
        self._deadline = time.monotonic() + self.limits.seconds

    # ---- budget ------------------------------------------------------

    @property
    def remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def spend(self, what: str) -> None:
        """Charge one action and record it. Raises when the budget is gone."""
        if self.remaining <= 0:
            raise AutomationError(
                f"The workflow ran out of time after {self.limits.seconds:.0f} seconds "
                f"at: {what}.",
                code="workflow_budget_exhausted",
            )
        if self.actions_used >= self.limits.actions:
            raise AutomationError(
                f"The workflow used its {self.limits.actions} allowed screen "
                f"actions without finishing, at: {what}.",
                code="workflow_budget_exhausted",
            )
        self.actions_used += 1
        self.steps.append(what)

    # ---- screen ------------------------------------------------------

    def look(self, why: str = "read the screen") -> UiSnapshot:
        self.spend(why)
        self.snapshot = self.backend.snapshot()
        return self.snapshot

    def settle(self) -> None:
        """Let an animation finish before reading the screen again."""
        time.sleep(min(self.limits.settle_seconds, self.remaining))

    def require(self, element_id: str) -> UiElement:
        """Resolve a reference against the *current* snapshot only."""
        if self.snapshot is None:
            raise AutomationError(
                "Nothing has been read from the screen yet.", code="stale_snapshot"
            )
        # Ids carry their snapshot, so one from an earlier screen cannot
        # accidentally match an element that now occupies the same index.
        if not element_id.startswith(f"{self.snapshot.snapshot_id}."):
            raise AutomationError(
                "That element came from an earlier screen. Read the screen "
                "again before acting on it.",
                code="stale_snapshot", retryable=True,
            )
        element = self.snapshot.get(element_id)
        if element is None:
            raise AutomationError(
                "That element is no longer on screen; the app has moved on.",
                code="stale_snapshot", retryable=True,
            )
        if not element.enabled:
            raise AutomationError(
                f"{element.label!r} is on screen but disabled.", code="target_not_found"
            )
        return element

    def unique(self, description: str, matches: list[UiElement]) -> UiElement:
        """Refuse to guess between candidates."""
        if not matches:
            raise AutomationError(
                f"Could not find {description} on this screen.",
                code="target_not_found", retryable=True,
            )
        if len(matches) > 1:
            labels = ", ".join(sorted({m.label for m in matches})[:4])
            raise AutomationError(
                f"{description} matched {len(matches)} things ({labels}); "
                "stopping rather than guessing.",
                code="target_ambiguous",
            )
        return matches[0]

    # ---- acting ------------------------------------------------------

    def tap(self, element: UiElement, why: str = "") -> None:
        self.spend(why or f"tap {element.label!r}")
        x, y = element.centre
        self.backend.tap(x, y)
        self.settle()

    def type_into(self, element: UiElement, text: str) -> None:
        if len(text) > self.limits.text_chars:
            raise AutomationError(
                f"That text is longer than the {self.limits.text_chars}-character "
                "limit for automated entry.",
                code="invalid_parameters",
            )
        if not element.editable:
            raise AutomationError(
                f"{element.label!r} is not a text field.", code="target_not_found"
            )
        self.spend(f"type into {element.label!r}")
        self.backend.tap(*element.centre)
        self.backend.type_text(text)
        self.settle()

    def press(self, key: str) -> None:
        self.spend(f"press {key}")
        self.backend.press(key)
        self.settle()

    def scroll(self, direction: str = "down") -> None:
        snapshot = self.snapshot
        self.spend(f"scroll {direction}")
        self.backend.scroll(
            direction,
            snapshot.width if snapshot else 1080,
            snapshot.height if snapshot else 1920,
        )
        self.settle()

    # ---- verifying ---------------------------------------------------

    def wait_until(self, predicate, description: str, timeout: float | None = None):
        """Poll for a screen state, bounded by both its own and the run budget.

        Returns the snapshot that satisfied the predicate. Raises rather than
        returning a half-ready screen, because every caller would otherwise
        have to remember to check.
        """
        limit = min(timeout or self.limits.state_timeout, self.remaining)
        finish_by = time.monotonic() + limit
        last: UiSnapshot | None = None
        while True:
            last = self.look(f"check for {description}")
            if predicate(last):
                return last
            if time.monotonic() >= finish_by:
                raise AutomationError(
                    f"Waited {limit:.0f}s but {description} never appeared. "
                    f"The screen showed: {last.visible_text(160)}",
                    code="state_timeout", retryable=True,
                )
            self.settle()

    def guard_interruptions(self, snapshot: UiSnapshot) -> None:
        """Stop on a login or challenge rather than trying to get past it.

        Working around an app's own safeguards is out of scope, and a
        half-understood login screen is exactly where blind automation does
        damage.
        """
        text = snapshot.visible_text(600).casefold()
        for needle, code, message in (
            ("captcha", "challenge_required", "the app is showing a challenge"),
            ("confirm it's you", "challenge_required", "the app wants you to confirm it is you"),
            ("verify it's you", "challenge_required", "the app wants you to verify it is you"),
            ("two-factor", "challenge_required", "the app is asking for a second factor"),
            ("enter the code", "challenge_required", "the app is asking for a code"),
            ("log in", "login_required", "the app is showing a login screen"),
            ("sign in", "login_required", "the app is showing a sign-in screen"),
        ):
            if needle in text:
                raise AutomationError(
                    f"Stopped because {message}. Please deal with it on the "
                    "phone yourself, then ask me again.",
                    code=code,
                )
