"""Allowlisted workflows.

A workflow is a named, reviewable recipe. The model chooses an id from this
registry and supplies schema-validated parameters; it never supplies a
package name, a URL, a coordinate or a step sequence. That is the whole
reason this is a registry rather than a general "do UI things" tool.

Everything here is read-only. Nothing likes, follows, posts, sends or buys.
Adding a workflow that does would mean giving it an approval-gated risk
level, not slipping it in beside these.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .backend import SUPPORTED_APPS
from .models import AutomationError, UiSnapshot, WorkflowResult
from .runtime import WorkflowRun

logger = logging.getLogger(__name__)

MAX_QUERY = 200
#: Instagram handles: letters, digits, dots, underscores, up to 30.
HANDLE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
COUNT_LABELS = ("followers", "following", "posts")


@dataclass(frozen=True)
class Workflow:
    workflow_id: str
    summary: str
    parameters: dict[str, Any]
    run: Callable[[WorkflowRun, dict[str, Any]], WorkflowResult]
    app: str = ""


def _query(parameters: dict[str, Any]) -> str:
    raw = str(parameters.get("query", "")).strip()
    if not raw:
        raise AutomationError("No search text was given.", code="invalid_parameters")
    if len(raw) > MAX_QUERY:
        raise AutomationError(
            f"That search is longer than {MAX_QUERY} characters.",
            code="invalid_parameters",
        )
    return raw


# ---------------------------------------------------------------- google

def google_search(run: WorkflowRun, parameters: dict[str, Any]) -> WorkflowResult:
    """Open a Google results page and confirm the results actually loaded."""
    query = _query(parameters)
    # Built here from an encoded query: the model never supplies a URL, so
    # there is no scheme, host or redirect it can influence.
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)

    run.spend(f"open Google results for {query!r}")
    run.backend.open_url(url)
    run.settle()

    def looks_loaded(snapshot: UiSnapshot) -> bool:
        text = snapshot.visible_text(1200).casefold()
        if "chrome" not in snapshot.package and "browser" not in snapshot.package:
            return False
        return any(token in text for token in query.casefold().split()[:3]) or "result" in text

    snapshot = run.wait_until(looks_loaded, f"search results for {query!r}")
    run.guard_interruptions(snapshot)

    headlines = [
        element.label for element in snapshot.elements
        if element.clickable and len(element.label) > 18
    ][:6]
    summary = (
        f"Opened Google for {query!r}. Visible on screen: "
        + ("; ".join(headlines) if headlines else snapshot.visible_text(300))
    )
    return WorkflowResult(
        True, summary,
        {"query": query, "url": url, "headlines": headlines},
        steps=list(run.steps),
    )


# ------------------------------------------------------------- instagram

def _instagram_handle(parameters: dict[str, Any]) -> str:
    # removeprefix, not lstrip: lstrip would turn "@@x" into the valid
    # handle "x", quietly accepting input the owner did not write.
    raw = str(parameters.get("username", "")).strip().removeprefix("@")
    if not HANDLE.match(raw):
        raise AutomationError(
            f"{raw!r} is not a usable Instagram username.", code="invalid_parameters"
        )
    return raw


def instagram_profile(run: WorkflowRun, parameters: dict[str, Any]) -> WorkflowResult:
    """Look up a profile and report only the counts actually on screen."""
    handle = _instagram_handle(parameters)

    run.spend(f"open Instagram for @{handle}")
    run.backend.launch(SUPPORTED_APPS["instagram"])
    run.settle()

    opened = run.wait_until(
        lambda snapshot: "instagram" in snapshot.package,
        "the Instagram app",
    )
    run.guard_interruptions(opened)

    search = run.unique(
        "the Instagram search box",
        opened.find(resource_id="action_bar_search_edit_text")
        or opened.find(text="search", editable=True)
        or opened.find(editable=True),
    )
    run.type_into(search, handle)

    results = run.wait_until(
        lambda snapshot: any(
            element.label.casefold().lstrip("@") == handle.casefold()
            for element in snapshot.elements
        ),
        f"a result for @{handle}",
    )
    run.guard_interruptions(results)

    # Open only an exact handle match. A near match is a different person.
    match = run.unique(
        f"the exact handle @{handle}",
        [
            element for element in results.elements
            if element.label.casefold().lstrip("@") == handle.casefold()
        ],
    )
    run.tap(match, f"open @{handle}")

    profile = run.wait_until(
        lambda snapshot: any(
            label in snapshot.visible_text(1200).casefold() for label in COUNT_LABELS
        ),
        f"the profile page for @{handle}",
    )
    run.guard_interruptions(profile)

    shown = profile.visible_text(1500)
    if handle.casefold() not in shown.casefold():
        raise AutomationError(
            f"Opened a profile but @{handle} is not shown on it, so the counts "
            "may belong to someone else. Stopping.",
            code="verification_failed",
        )

    counts = _read_counts(profile)
    missing = [label for label in ("followers", "following") if label not in counts]
    summary = f"@{handle}: " + ", ".join(
        f"{value} {label}" for label, value in counts.items()
    ) if counts else f"@{handle}: no counts were visible on the profile."
    if missing:
        summary += f" ({' and '.join(missing)} not visible on screen.)"
    # Values like 1.2K are reported as displayed: Instagram rounds them, and
    # converting would invent precision the screen never showed.
    return WorkflowResult(
        True, summary,
        {"handle": handle, "counts": counts, "as_displayed": True},
        steps=list(run.steps),
    )


def _read_counts(snapshot: UiSnapshot) -> dict[str, str]:
    """Pair each count label with the value shown beside it.

    Values are kept exactly as displayed, including 1.2K: Instagram rounds
    them, so converting to 1200 would state a precision the screen never did.
    """
    labels = [
        element for element in snapshot.elements
        if element.label.casefold().strip() in COUNT_LABELS
    ]
    counts: dict[str, str] = {}
    for label_element in labels:
        name = label_element.label.casefold().strip()
        left, top, right, bottom = label_element.bounds
        best, best_distance = None, None
        for candidate in snapshot.elements:
            value = candidate.label.strip()
            if candidate is label_element or not value:
                continue
            if not re.fullmatch(r"[\d,.]+[KkMm]?", value):
                continue
            c_left, c_top, c_right, c_bottom = candidate.bounds
            # The number sits directly above or beside its label.
            horizontal = abs((c_left + c_right) // 2 - (left + right) // 2)
            vertical = abs((c_top + c_bottom) // 2 - (top + bottom) // 2)
            distance = horizontal + vertical * 2
            if horizontal > 260 or vertical > 220:
                continue
            if best_distance is None or distance < best_distance:
                best, best_distance = value, distance
        if best:
            counts[name] = best
    return counts


WORKFLOWS: dict[str, Workflow] = {
    "google_search": Workflow(
        "google_search",
        "Search Google in the browser and report what is on screen.",
        {
            "query": {
                "type": "string", "minLength": 1, "maxLength": MAX_QUERY,
                "description": "What to search for.",
            }
        },
        google_search,
        app="chrome",
    ),
    "instagram_profile": Workflow(
        "instagram_profile",
        "Open an Instagram profile and read the follower and following "
        "counts that are visible. Read only: it never follows, likes or posts.",
        {
            "username": {
                "type": "string", "minLength": 1, "maxLength": 31,
                "description": "The handle, with or without the leading @.",
            }
        },
        instagram_profile,
        app="instagram",
    ),
}


def describe_workflows() -> str:
    return "; ".join(
        f"{workflow.workflow_id} ({workflow.summary.rstrip('.')})"
        for workflow in WORKFLOWS.values()
    )
