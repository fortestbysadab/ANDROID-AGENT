"""Tests for app automation.

The point of this subsystem is that a language model drives other apps
*without* being handed a general-purpose tap-anywhere tool. Most of these
tests are therefore about refusal: stale references, ambiguous matches,
budgets, login screens, and never reporting success the screen does not
support.
"""

from __future__ import annotations

import unittest
from unittest import mock

from android_agent.automation import (
    AutomationError,
    FakeBackend,
    Limits,
    WorkflowRun,
    parse_hierarchy,
    screen,
)
from android_agent.automation.workflows import WORKFLOWS, _read_counts
from android_agent.tools.app_tools import app_tools
from android_agent.tools.base import Risk

HIERARCHY = """<?xml version="1.0"?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout"
        package="com.android.chrome" bounds="[0,0][1080,1920]" clickable="false"/>
  <node index="1" text="Search or type URL" resource-id="com.android.chrome:id/search_box"
        class="android.widget.EditText" package="com.android.chrome"
        bounds="[60,200][1020,300]" clickable="true" enabled="true"/>
  <node index="2" text="India - Wikipedia" resource-id="" class="android.widget.TextView"
        package="com.android.chrome" bounds="[40,400][1040,470]" clickable="true" enabled="true"/>
</hierarchy>"""


class ParsingTests(unittest.TestCase):
    def test_a_dump_becomes_addressable_elements(self):
        snapshot = parse_hierarchy(HIERARCHY)
        self.assertEqual(snapshot.package, "com.android.chrome")
        self.assertEqual(len(snapshot.elements), 2)

    def test_structural_nodes_without_labels_are_dropped(self):
        self.assertNotIn(
            "FrameLayout", [e.class_name for e in parse_hierarchy(HIERARCHY).elements]
        )

    def test_elements_can_be_found_by_resource_id(self):
        """Preferred over text: a resource id survives a language change."""
        snapshot = parse_hierarchy(HIERARCHY)
        found = snapshot.find(resource_id="search_box")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].editable)

    def test_a_dtd_is_refused(self):
        """Screen text is app-controlled, so the XML is untrusted input."""
        with self.assertRaises(AutomationError) as caught:
            parse_hierarchy('<!DOCTYPE x [<!ENTITY a "boom">]><hierarchy/>')
        self.assertEqual(caught.exception.code, "unsupported_screen")

    def test_an_implausibly_large_dump_is_refused(self):
        with self.assertRaises(AutomationError):
            parse_hierarchy("<hierarchy>" + " " * (5 * 1024 * 1024) + "</hierarchy>")

    def test_unparseable_xml_fails_cleanly(self):
        with self.assertRaises(AutomationError) as caught:
            parse_hierarchy("<hierarchy><node")
        self.assertEqual(caught.exception.code, "unsupported_screen")

    def test_node_count_is_capped(self):
        nodes = "".join(
            f'<node index="{i}" text="row {i}" class="T" package="p" '
            f'bounds="[0,{i}][100,{i + 10}]" clickable="true"/>'
            for i in range(600)
        )
        snapshot = parse_hierarchy(f"<hierarchy>{nodes}</hierarchy>")
        self.assertLessEqual(len(snapshot.elements), 300)
        self.assertTrue(snapshot.truncated)


class RunBudgetTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend([screen("Hello")])

    def test_actions_are_capped(self):
        run = WorkflowRun(self.backend, Limits(actions=3))
        for _ in range(3):
            run.spend("step")
        with self.assertRaises(AutomationError) as caught:
            run.spend("one too many")
        self.assertEqual(caught.exception.code, "workflow_budget_exhausted")

    def test_time_is_capped(self):
        run = WorkflowRun(self.backend, Limits(seconds=0.0))
        with self.assertRaises(AutomationError) as caught:
            run.spend("anything")
        self.assertEqual(caught.exception.code, "workflow_budget_exhausted")

    def test_the_failure_names_the_step_it_stopped_on(self):
        run = WorkflowRun(self.backend, Limits(actions=1))
        run.spend("open the app")
        with self.assertRaises(AutomationError) as caught:
            run.spend("tap the search box")
        self.assertIn("tap the search box", str(caught.exception))


class ReferenceTests(unittest.TestCase):
    """Element references are snapshot-scoped, never durable."""

    def test_a_reference_from_an_old_screen_is_refused(self):
        first = screen("Old button")
        second = screen("Something else entirely")
        backend = FakeBackend([first, second])
        run = WorkflowRun(backend)
        run.look()
        stale_id = first.elements[0].element_id
        run.look()  # the screen moved on
        with self.assertRaises(AutomationError) as caught:
            run.require(stale_id)
        self.assertEqual(caught.exception.code, "stale_snapshot")
        # Two mechanisms protect this: ids are globally unique, and the
        # reference is checked against the current snapshot. The message
        # should say which situation this is, not just that it failed.
        self.assertIn("earlier screen", str(caught.exception))

    def test_an_id_from_another_screen_cannot_collide_with_this_one(self):
        """Index-based ids would make e0 mean something different each screen."""
        first, second = screen("Old"), screen("New")
        self.assertNotEqual(
            first.elements[0].element_id, second.elements[0].element_id
        )

    def test_acting_before_reading_is_refused(self):
        run = WorkflowRun(FakeBackend([screen("x")]))
        with self.assertRaises(AutomationError) as caught:
            run.require("e0")
        self.assertEqual(caught.exception.code, "stale_snapshot")

    def test_an_ambiguous_match_stops_rather_than_guessing(self):
        snapshot = screen("Follow", "Follow", "Message")
        run = WorkflowRun(FakeBackend([snapshot]))
        run.look()
        with self.assertRaises(AutomationError) as caught:
            run.unique("the follow button", snapshot.find(text="Follow"))
        self.assertEqual(caught.exception.code, "target_ambiguous")
        self.assertIn("rather than guessing", str(caught.exception))

    def test_a_missing_target_is_reported_not_invented(self):
        snapshot = screen("Nothing useful")
        run = WorkflowRun(FakeBackend([snapshot]))
        with self.assertRaises(AutomationError) as caught:
            run.unique("the search box", snapshot.find(text="search"))
        self.assertEqual(caught.exception.code, "target_not_found")

    def test_typing_into_a_non_field_is_refused(self):
        snapshot = screen("Just a label")
        run = WorkflowRun(FakeBackend([snapshot]))
        run.look()
        with self.assertRaises(AutomationError):
            run.type_into(snapshot.elements[0], "hello")

    def test_overlong_text_is_refused(self):
        snapshot = screen("Field", editable={"Field"})
        run = WorkflowRun(FakeBackend([snapshot]), Limits(text_chars=10))
        run.look()
        with self.assertRaises(AutomationError) as caught:
            run.type_into(snapshot.elements[0], "x" * 50)
        self.assertEqual(caught.exception.code, "invalid_parameters")


class InterruptionTests(unittest.TestCase):
    def test_a_login_screen_stops_the_workflow(self):
        run = WorkflowRun(FakeBackend())
        with self.assertRaises(AutomationError) as caught:
            run.guard_interruptions(screen("Log in to continue", "Username"))
        self.assertEqual(caught.exception.code, "login_required")

    def test_a_challenge_stops_the_workflow(self):
        run = WorkflowRun(FakeBackend())
        for text in ("Confirm it's you", "Enter the code we sent", "CAPTCHA"):
            with self.subTest(text=text), self.assertRaises(AutomationError) as caught:
                run.guard_interruptions(screen(text))
            self.assertEqual(caught.exception.code, "challenge_required")

    def test_an_ordinary_screen_passes(self):
        WorkflowRun(FakeBackend()).guard_interruptions(screen("Followers", "1,234"))


class GoogleWorkflowTests(unittest.TestCase):
    def test_a_search_opens_an_encoded_url_and_verifies_the_result(self):
        results = screen(
            "India - Wikipedia, the free encyclopedia",
            "India travel guide for visitors",
            package="com.android.chrome",
        )
        backend = FakeBackend([results])
        run = WorkflowRun(backend, Limits(settle_seconds=0))
        outcome = WORKFLOWS["google_search"].run(run, {"query": "India"})
        self.assertTrue(outcome.ok)
        self.assertEqual(
            backend.urls, ["https://www.google.com/search?q=India"]
        )
        self.assertIn("Wikipedia", outcome.summary)

    def test_the_reply_names_the_browser_that_handled_it(self):
        """Android routes the URL to the default browser, not the Google app."""
        backend = FakeBackend([screen("India - Wikipedia page", package="com.android.chrome")])
        run = WorkflowRun(backend, Limits(settle_seconds=0))
        outcome = WORKFLOWS["google_search"].run(run, {"query": "India"})
        self.assertIn("Chrome", outcome.summary)
        self.assertEqual(outcome.data["browser"], "com.android.chrome")

    def test_an_unknown_browser_is_named_by_its_package(self):
        backend = FakeBackend([screen("India results here", package="com.some.browser")])
        run = WorkflowRun(backend, Limits(settle_seconds=0))
        outcome = WORKFLOWS["google_search"].run(run, {"query": "India"})
        self.assertIn("com.some.browser", outcome.summary)

    def test_the_query_is_url_encoded_not_interpolated(self):
        backend = FakeBackend([screen("tea & coffee results", package="com.android.chrome")])
        run = WorkflowRun(backend, Limits(settle_seconds=0))
        WORKFLOWS["google_search"].run(run, {"query": "tea & coffee"})
        self.assertIn("tea+%26+coffee", backend.urls[0])

    def test_an_empty_query_is_refused_before_anything_opens(self):
        backend = FakeBackend([screen("x")])
        run = WorkflowRun(backend, Limits(settle_seconds=0))
        with self.assertRaises(AutomationError) as caught:
            WORKFLOWS["google_search"].run(run, {"query": "   "})
        self.assertEqual(caught.exception.code, "invalid_parameters")
        self.assertEqual(backend.urls, [])

    def test_results_that_never_load_time_out_rather_than_claim_success(self):
        backend = FakeBackend([screen("Blank page", package="com.other.app")])
        run = WorkflowRun(backend, Limits(seconds=1.5, settle_seconds=0, state_timeout=1.0))
        with self.assertRaises(AutomationError) as caught:
            WORKFLOWS["google_search"].run(run, {"query": "India"})
        self.assertIn(caught.exception.code, {"state_timeout", "workflow_budget_exhausted"})


class InstagramWorkflowTests(unittest.TestCase):
    IG = "com.instagram.android"

    def _profile_screen(self, handle="natgeo", followers="283M", following="134"):
        snapshot = screen(
            f"@{handle}", followers, "followers", following, "following",
            package=self.IG,
        )
        return snapshot

    def test_counts_are_paired_with_their_labels(self):
        counts = _read_counts(self._profile_screen())
        self.assertEqual(counts.get("followers"), "283M")
        self.assertEqual(counts.get("following"), "134")

    def test_abbreviated_counts_are_kept_as_displayed(self):
        """1.2K is what the screen said; 1200 would invent precision."""
        counts = _read_counts(
            screen("@someone", "1.2K", "followers", package=self.IG)
        )
        self.assertEqual(counts.get("followers"), "1.2K")

    def test_a_malformed_handle_is_refused(self):
        for bad in ("", "not a handle", "@@x", "x" * 40):
            # A fresh run per case: sharing one would exhaust its budget and
            # the later cases would pass for the wrong reason.
            run = WorkflowRun(FakeBackend([screen("x")]), Limits(settle_seconds=0))
            with self.subTest(handle=bad), self.assertRaises(AutomationError) as caught:
                WORKFLOWS["instagram_profile"].run(run, {"username": bad})
            self.assertEqual(caught.exception.code, "invalid_parameters")

    def test_a_near_miss_handle_is_not_opened(self):
        """natgeo and natgeo_official are different people."""
        listing = screen("@natgeo_official", "@natgeotravel", package=self.IG)
        backend = FakeBackend([screen("Search", editable={"Search"}, package=self.IG), listing])
        run = WorkflowRun(backend, Limits(seconds=2, settle_seconds=0, state_timeout=0.5))
        with self.assertRaises(AutomationError) as caught:
            WORKFLOWS["instagram_profile"].run(run, {"username": "natgeo"})
        self.assertIn(caught.exception.code, {"state_timeout", "workflow_budget_exhausted"})

    def test_a_login_screen_stops_before_searching(self):
        backend = FakeBackend([screen("Log in", "Username", package=self.IG)])
        run = WorkflowRun(backend, Limits(seconds=2, settle_seconds=0, state_timeout=0.5))
        with self.assertRaises(AutomationError) as caught:
            WORKFLOWS["instagram_profile"].run(run, {"username": "natgeo"})
        self.assertEqual(caught.exception.code, "login_required")
        self.assertEqual(backend.typed, [], "nothing should have been typed")


class ToolSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend([screen("Hello", package="com.android.chrome")])
        self.tools = {tool.name: tool for tool in app_tools(self.backend)}

    def call(self, name, **arguments):
        tool = self.tools[name]
        return tool.handler(None, tool.validate(arguments))

    def test_there_is_no_tap_anywhere_tool(self):
        """The whole design rests on this."""
        self.assertEqual(set(self.tools), {"run_app_workflow", "read_app_screen"})

    def test_only_allowlisted_workflows_are_offered(self):
        enum = self.tools["run_app_workflow"].input_schema["properties"]["workflow"]["enum"]
        self.assertEqual(set(enum), set(WORKFLOWS))

    def test_an_unknown_workflow_is_rejected_by_the_schema(self):
        from android_agent.tools.base import SchemaValidationError

        with self.assertRaises(SchemaValidationError):
            self.tools["run_app_workflow"].validate({"workflow": "delete_everything"})

    def test_a_package_name_cannot_be_supplied(self):
        from android_agent.tools.base import SchemaValidationError

        with self.assertRaises(SchemaValidationError):
            self.tools["run_app_workflow"].validate(
                {"workflow": "google_search", "package": "com.bank.app"}
            )

    def test_a_url_cannot_be_supplied(self):
        from android_agent.tools.base import SchemaValidationError

        with self.assertRaises(SchemaValidationError):
            self.tools["run_app_workflow"].validate(
                {"workflow": "google_search", "url": "https://evil.example"}
            )

    def test_screen_content_is_untrusted(self):
        for name in ("run_app_workflow", "read_app_screen"):
            with self.subTest(tool=name):
                self.assertTrue(self.tools[name].returns_untrusted_content)

    def test_driving_the_screen_is_raw_control(self):
        self.assertEqual(self.tools["run_app_workflow"].risk, Risk.RAW_CONTROL)
        self.assertEqual(self.tools["read_app_screen"].risk, Risk.SENSITIVE_READ)

    def test_an_unavailable_backend_explains_itself(self):
        self.backend.unavailable = "Wireless ADB is not connected."
        result = self.call("run_app_workflow", workflow="google_search", query="x")
        self.assertEqual(result.error_code, "backend_unavailable")
        self.assertIn("ADB", result.summary)

    def test_reading_the_screen_marks_it_untrusted(self):
        result = self.call("read_app_screen")
        self.assertEqual(result.status, "ok")
        self.assertIn("UNTRUSTED", result.summary)

    def test_a_failure_reports_how_far_it_got(self):
        self.backend.screens = [screen("Nothing here", package="com.other")]
        with mock.patch(
            "android_agent.tools.app_tools.Limits",
            return_value=Limits(seconds=1.0, settle_seconds=0, state_timeout=0.4),
        ):
            result = self.call("run_app_workflow", workflow="google_search", query="India")
        self.assertEqual(result.status, "error")
        self.assertIn("What happened first", result.summary)


if __name__ == "__main__":
    unittest.main()
