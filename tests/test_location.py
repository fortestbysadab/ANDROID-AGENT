"""Tests for get_location.

Every case here is a failure mode reported against termux-location upstream:
exit 0 with empty output, an API_ERROR object instead of coordinates, a cold
GPS start that takes longer than any sane timeout, and a missing permission.
The old implementation turned all of them into one unhelpful error.
"""

from __future__ import annotations

import json
import time
import unittest
from typing import ClassVar
from unittest import mock

from android_agent.tools import termux_extra
from android_agent.tools.catalog import build_full_registry

FIX = {
    "latitude": 22.5726,
    "longitude": 88.3639,
    "accuracy": 18.0,
    "altitude": 9.0,
    "bearing": 0.0,
    "speed": 0.0,
    "elapsedMs": 12,
    "provider": "network",
}


def spec():
    return build_full_registry().get("get_location")


def run(arguments=None):
    return termux_extra._location(None, arguments or {})


def live_calls(fake):
    """Providers asked for a *new* fix - the throttled kind."""
    return [provider for provider, request, _ in fake.calls if request == "once"]


class FakeTermux:
    """Stands in for _run, keyed by (provider, request)."""

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple[str, str, float]] = []
        self.active = 0
        self.max_concurrent = 0

    def __call__(self, args, timeout=12.0, **kwargs):
        self.kwargs = kwargs
        provider, request = args[2], args[4]
        self.active += 1
        self.max_concurrent = max(self.max_concurrent, self.active)
        try:
            self.calls.append((provider, request, timeout))
            reply = self.responses.get((provider, request), (True, ""))
            return reply(self) if callable(reply) else reply
        finally:
            self.active -= 1


class patched:
    """Patch the Termux shell-out and clear the live-request cooldown.

    The cooldown is deliberately module-global state (a circuit breaker for a
    device that keeps killing helper processes), so it has to be reset between
    tests or one failing case silently changes the next one's behaviour.
    """

    def __init__(self, responses):
        self.fake = FakeTermux(responses)
        self.patcher = mock.patch.object(termux_extra, "_run", self.fake)

    def __enter__(self):
        termux_extra.reset_live_cooldown()
        self.patcher.start()
        return self.fake

    def __exit__(self, *exc):
        self.patcher.stop()
        termux_extra.reset_live_cooldown()
        return False


class LocationSuccessTests(unittest.TestCase):
    def test_gps_is_asked_first(self):
        gps = dict(FIX, provider="gps", accuracy=8.0)
        with patched({("gps", "once"): (True, json.dumps(gps))}) as fake:
            result = run()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["provider"], "gps")
        self.assertEqual(fake.calls[0][0], "gps")

    def test_a_good_gps_fix_stops_there_and_never_touches_the_network(self):
        """One request at a time is what keeps Termux:API from erroring."""
        gps = dict(FIX, provider="gps", accuracy=8.0)
        with patched({("gps", "once"): (True, json.dumps(gps))}) as fake:
            run()
        self.assertNotIn(("network", "once"), [(p, r) for p, r, _ in fake.calls])

    def test_network_is_used_only_when_gps_gives_nothing(self):
        with patched({
            ("gps", "once"): (True, ""),
            ("network", "once"): (True, json.dumps(FIX)),
        }) as fake:
            result = run()
        self.assertEqual(result.data["provider"], "network")
        self.assertEqual(live_calls(fake), ["gps", "network"])

    def test_summary_is_human_readable(self):
        with patched({("network", "once"): (True, json.dumps(FIX))}):
            result = run()
        self.assertIn("22.57260", result.summary)
        self.assertIn("18 m", result.summary)
        self.assertIn("network", result.summary)

    def test_requested_provider_is_used_alone(self):
        with patched({("network", "once"): (True, json.dumps(FIX))}) as fake:
            result = run({"provider": "network"})
        self.assertEqual(result.data["provider"], "network")
        self.assertEqual({call[0] for call in fake.calls}, {"network"})

    def test_a_recent_cached_fix_is_used_without_a_live_request(self):
        """The fix for the real bug: -r once is throttled in the background."""
        recent = dict(FIX, provider="gps", accuracy=8.0, elapsedMs=3000)
        with patched({("gps", "last"): (True, json.dumps(recent))}) as fake:
            result = run()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["accuracy"], 8.0)
        self.assertFalse(result.data["stale"])
        self.assertEqual(live_calls(fake), [])

    def test_an_old_cached_fix_does_not_short_circuit_a_live_request(self):
        old = dict(FIX, provider="gps", accuracy=8.0, elapsedMs=30 * 60 * 1000)
        fresh = dict(FIX, provider="gps", accuracy=9.0, elapsedMs=100)
        with patched({
            ("gps", "last"): (True, json.dumps(old)),
            ("gps", "once"): (True, json.dumps(fresh)),
        }) as fake:
            result = run()
        self.assertEqual(result.data["accuracy"], 9.0)
        self.assertIn("gps", live_calls(fake))

    def test_a_coarse_cached_fix_does_not_short_circuit_a_live_request(self):
        coarse = dict(FIX, provider="network", accuracy=800.0, elapsedMs=1000)
        with patched({
            ("network", "last"): (True, json.dumps(coarse)),
            ("gps", "once"): (True, json.dumps(dict(FIX, provider="gps", accuracy=8.0))),
        }) as fake:
            result = run()
        self.assertEqual(result.data["accuracy"], 8.0)
        self.assertIn("gps", live_calls(fake))

    def test_gps_gets_a_longer_budget_than_network(self):
        plan = dict(termux_extra._LOCATION_PLAN["balanced"])
        self.assertGreater(plan["gps"], plan["network"])

    def test_tool_timeout_exceeds_the_worst_case_plan(self):
        """A tool killed by the runtime mid-fix would waste the whole wait."""
        worst = max(
            sum(seconds for _, seconds in plan)
            for plan in termux_extra._LOCATION_PLAN.values()
        )
        self.assertGreater(spec().timeout_seconds, worst)


class LocationFallbackTests(unittest.TestCase):
    def test_falls_back_to_the_other_provider(self):
        with patched({
            ("gps", "once"): (False, "termux-location timed out after 25s"),
            ("network", "once"): (True, json.dumps(FIX)),
        }) as fake:
            result = run()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["provider"], "network")
        self.assertEqual(live_calls(fake), ["gps", "network"])

    def test_falls_back_to_a_cached_fix_and_flags_it_stale(self):
        cached = dict(FIX, elapsedMs=15 * 60 * 1000)
        with patched({
            ("network", "once"): (True, ""),
            ("gps", "once"): (True, ""),
            ("network", "last"): (True, json.dumps(cached)),
        }) as fake:
            result = run()
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.data["stale"])
        self.assertEqual(result.data["fix_age_seconds"], 900.0)
        self.assertIn("last known fix", result.summary)
        self.assertIn("15 min old", result.summary)
        self.assertIn(("network", "last", termux_extra._LOCATION_LAST_TIMEOUT), fake.calls)

    def test_a_recent_cached_fix_is_not_called_stale_in_the_summary(self):
        cached = dict(FIX, elapsedMs=5000)
        with patched({
            ("network", "once"): (True, ""),
            ("gps", "once"): (True, ""),
            ("network", "last"): (True, json.dumps(cached)),
        }):
            result = run()
        self.assertFalse(result.data["stale"])
        self.assertNotIn("last known", result.summary)

    def test_a_live_fix_wins_when_the_cache_is_worse(self):
        stale_coarse = dict(FIX, accuracy=900.0, elapsedMs=60 * 60 * 1000)
        with patched({
            ("gps", "last"): (True, json.dumps(stale_coarse)),
            ("gps", "once"): (True, json.dumps(dict(FIX, accuracy=8.0, elapsedMs=50))),
        }):
            result = run()
        self.assertEqual(result.data["accuracy"], 8.0)
        self.assertFalse(result.data["stale"])


class LocationAccuracyTests(unittest.TestCase):
    """The reported position must be the most accurate one available.

    A cell-tower network fix is routinely 600 m to several km wide. Returning
    whichever provider answered first meant that estimate won every time,
    because GPS is always slower.
    """

    COARSE: ClassVar[dict] = dict(FIX, accuracy=2100.0, provider="network")
    FINE: ClassVar[dict] = dict(FIX, accuracy=12.0, provider="gps")

    def test_gps_beats_a_kilometre_wide_network_fix(self):
        with patched({
            ("network", "once"): (True, json.dumps(self.COARSE)),
            ("gps", "once"): (True, json.dumps(self.FINE)),
        }):
            result = run()
        self.assertEqual(result.data["provider"], "gps")
        self.assertEqual(result.data["accuracy"], 12.0)
        self.assertFalse(result.data["approximate"])

    def test_providers_are_never_queried_concurrently(self):
        """Overlapping termux-location calls make Termux:API fail to deliver."""
        with patched({
            ("gps", "once"): (True, json.dumps(self.COARSE)),
            ("network", "once"): (True, json.dumps(self.COARSE)),
        }) as fake:
            run()
        self.assertGreaterEqual(len(fake.calls), 2)
        self.assertEqual(fake.max_concurrent, 1)

    def test_a_coarse_fix_is_still_returned_when_it_is_all_there_is(self):
        with patched({
            ("gps", "once"): (True, ""),
            ("network", "once"): (True, json.dumps(self.COARSE)),
        }):
            result = run()
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.data["approximate"])

    def test_a_coarse_fix_says_so_in_kilometres(self):
        with patched({
            ("gps", "once"): (True, ""),
            ("network", "once"): (True, json.dumps(self.COARSE)),
        }):
            summary = run().summary
        self.assertIn("2.1 km", summary)
        self.assertIn("coarse", summary.lower())
        self.assertIn("precise", summary)

    def test_a_precise_fix_carries_no_warning(self):
        with patched({("gps", "once"): (True, json.dumps(self.FINE))}):
            summary = run({"provider": "gps"}).summary
        self.assertNotIn("coarse", summary.lower())
        self.assertIn("12 m", summary)

    def test_fix_without_stated_accuracy_counts_as_approximate(self):
        blind = {k: v for k, v in FIX.items() if k != "accuracy"}
        with patched({
            ("gps", "once"): (True, ""),
            ("network", "once"): (True, json.dumps(blind)),
        }):
            result = run()
        self.assertTrue(result.data["approximate"])

    def test_an_explicit_provider_is_not_second_guessed(self):
        with patched({
            ("network", "once"): (True, json.dumps(self.COARSE)),
            ("gps", "once"): (True, json.dumps(self.FINE)),
        }) as fake:
            result = run({"provider": "network"})
        self.assertEqual({c[0] for c in fake.calls}, {"network"})
        self.assertEqual(result.data["provider"], "network")

    def test_precision_levels_give_gps_different_budgets(self):
        budgets = {
            name: dict(plan)["gps"] for name, plan in termux_extra._LOCATION_PLAN.items()
        }
        self.assertLess(budgets["fast"], budgets["balanced"])
        self.assertLess(budgets["balanced"], budgets["precise"])

    def test_tighter_precision_demands_tighter_accuracy(self):
        target = termux_extra._LOCATION_TARGET
        self.assertGreater(target["fast"], target["balanced"])
        self.assertGreater(target["balanced"], target["precise"])

    def test_every_precision_leads_with_gps(self):
        for name, plan in termux_extra._LOCATION_PLAN.items():
            with self.subTest(precision=name):
                self.assertEqual(plan[0][0], "gps")

    def test_unknown_precision_falls_back_to_balanced(self):
        with patched({("gps", "once"): (True, json.dumps(self.FINE))}):
            result = run({"precision": "nonsense"})
        self.assertEqual(result.status, "ok")


class LocationFailureTests(unittest.TestCase):
    """Upstream termux-location lies about success in several ways."""

    def test_empty_output_with_exit_zero_is_not_treated_as_data(self):
        with patched({}):
            result = run()
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, "location_unavailable")
        self.assertIn("no fix returned", result.summary)

    def test_api_error_object_is_surfaced(self):
        with patched({
            ("network", "once"): (True, '{"API_ERROR": "Failed to get location"}'),
            ("gps", "once"): (True, '{"API_ERROR": "Failed to get location"}'),
        }):
            result = run()
        self.assertEqual(result.status, "error")
        self.assertIn("Failed to get location", result.summary)

    def test_json_without_coordinates_is_rejected(self):
        with patched({("network", "once"): (True, '{"provider": "network"}')}):
            result = run()
        self.assertEqual(result.status, "error")
        self.assertIn("no coordinates", result.summary)

    def test_unparseable_output_is_rejected(self):
        with patched({("network", "once"): (True, "not json at all")}):
            result = run()
        self.assertEqual(result.status, "error")
        self.assertIn("unreadable", result.summary)

    def test_error_explains_permissions_and_background_throttling(self):
        with patched({}):
            result = run()
        self.assertIn("Location permission", result.summary)
        self.assertIn("Termux:API", result.summary)
        self.assertIn("background apps", result.summary)
        self.assertIn("termux-wake-lock", result.summary)

    def test_error_is_retryable(self):
        with patched({}):
            self.assertTrue(run().retryable)

    def test_missing_binary_is_reported_clearly(self):
        with patched({
            ("network", "once"): (False, "termux-location is not installed"),
            ("gps", "once"): (False, "termux-location is not installed"),
        }):
            result = run()
        self.assertIn("not installed", result.summary)

    def test_every_provider_is_attempted_before_giving_up(self):
        with patched({}) as fake:
            run()
        attempted = {(provider, request) for provider, request, _ in fake.calls}
        self.assertIn(("network", "once"), attempted)
        self.assertIn(("gps", "once"), attempted)
        self.assertIn(("passive", "last"), attempted)


class RealDeviceTests(unittest.TestCase):
    """Reproduces the exact readings from the owner's phone.

    GPS: 8 m at 22.364643, 87.999725. Network: 800 m at 22.378115, 87.985383.
    The two are about 1.8 km apart, which is the error that was reported.
    """

    GPS: ClassVar[dict] = {
        "latitude": 22.364643333333333, "longitude": 87.999725, "altitude": -53.7,
        "accuracy": 8.0, "vertical_accuracy": 61.5, "bearing": 309.87,
        "speed": 7.1016, "elapsedMs": 120, "provider": "gps",
    }
    NETWORK: ClassVar[dict] = {
        "latitude": 22.3781152, "longitude": 87.9853832, "altitude": 0.0,
        "accuracy": 800.0, "vertical_accuracy": 0.0, "bearing": 0.0,
        "speed": 0.0, "elapsedMs": 11, "provider": "network",
    }

    def test_the_gps_reading_is_the_one_reported(self):
        with patched({
            ("gps", "once"): (True, json.dumps(self.GPS)),
            ("network", "once"): (True, json.dumps(self.NETWORK)),
        }):
            result = run()
        self.assertEqual(result.data["provider"], "gps")
        self.assertEqual(result.data["provider"], "gps")
        self.assertAlmostEqual(result.data["latitude"], 22.364643333333333)
        self.assertEqual(result.data["accuracy"], 8.0)
        self.assertFalse(result.data["approximate"])
        self.assertIn("8 m", result.summary)

    def test_the_800m_network_reading_would_be_flagged_on_its_own(self):
        with patched({
            ("gps", "once"): (True, ""),
            ("network", "once"): (True, json.dumps(self.NETWORK)),
        }):
            result = run()
        self.assertTrue(result.data["approximate"])
        self.assertIn("800 m", result.summary)
        self.assertIn("coarse", result.summary.lower())

    def test_gps_wins_a_tie_on_stated_accuracy(self):
        """Ranking preference, independent of who replied first.

        The race deliberately stops early on any fix that already meets the
        accuracy target - an 8 m network fix is genuinely good enough and not
        worth another 60 seconds of GPS. The tie-break decides only between
        results that are both already in hand.
        """
        gps = termux_extra._location_payload(self.GPS, "gps", "once")
        network = termux_extra._location_payload(
            dict(self.NETWORK, accuracy=8.0), "network", "once"
        )
        self.assertLess(termux_extra._location_rank(gps), termux_extra._location_rank(network))

    def test_the_8m_gps_fix_short_circuits_everything_else(self):
        """The owner's warm GPS answers instantly; nothing else is needed."""
        with patched({
            ("gps", "once"): (True, json.dumps(self.GPS)),
            ("network", "once"): (True, json.dumps(self.NETWORK)),
        }) as fake:
            result = run()
        self.assertEqual(result.data["accuracy"], 8.0)
        self.assertEqual(live_calls(fake), ["gps"])

    def test_the_same_gps_reading_from_cache_needs_no_live_request(self):
        """elapsedMs 120 means the fix is 0.12s old - as good as live."""
        with patched({("gps", "last"): (True, json.dumps(self.GPS))}) as fake:
            result = run()
        self.assertEqual(result.data["accuracy"], 8.0)
        self.assertFalse(result.data["stale"])
        self.assertEqual(live_calls(fake), [])


class LocationServicesOffTests(unittest.TestCase):
    """Location switched off is a distinct condition with a distinct answer."""

    DISABLED = (False, "gps provider is disabled")

    def test_disabled_providers_produce_a_specific_error(self):
        with patched({
            ("gps", "once"): self.DISABLED,
            ("network", "once"): self.DISABLED,
            ("gps", "last"): self.DISABLED,
            ("network", "last"): self.DISABLED,
            ("passive", "last"): self.DISABLED,
        }):
            result = run()
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, "location_services_off")
        self.assertIn("Location services appear to be switched off", result.summary)
        self.assertIn("quick settings", result.summary)

    def test_a_generic_failure_is_not_blamed_on_the_location_switch(self):
        with patched({}):
            result = run()
        self.assertEqual(result.error_code, "location_unavailable")
        self.assertNotIn("switched off", result.summary)

    def test_cached_fix_is_used_as_the_fallback_when_location_is_off(self):
        cached = dict(FIX, accuracy=40.0, elapsedMs=8 * 60 * 1000, provider="gps")
        with patched({
            ("gps", "once"): self.DISABLED,
            ("network", "once"): self.DISABLED,
            ("gps", "last"): (True, json.dumps(cached)),
        }):
            result = run()
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.data["stale"])
        self.assertTrue(result.data["live_fix_failed"])
        self.assertIn("last known fix", result.summary)
        self.assertIn("8 min old", result.summary)
        self.assertIn("switched off", result.summary)

    def test_a_live_fix_is_never_marked_as_a_failed_one(self):
        with patched({("network", "once"): (True, json.dumps(FIX))}):
            result = run()
        self.assertNotIn("live_fix_failed", result.data)

    def test_cached_fallback_is_tried_for_every_provider(self):
        with patched({}) as fake:
            run()
        cached = [provider for provider, request, _ in fake.calls if request == "last"]
        self.assertEqual(cached, ["gps", "network", "passive"])


class BackgroundThrottlingTests(unittest.TestCase):
    """Android gives background apps a new fix only a few times an hour.

    That is the difference between `termux-location` in the terminal (Termux
    on screen, instant) and the same call from the agent (Termux behind a chat
    app, stalls until the timeout). The cached read is not throttled, so it is
    tried first and is what makes the common case fast.
    """

    def test_cached_reads_happen_before_any_live_request(self):
        with patched({}) as fake:
            run()
        first = fake.calls[0]
        self.assertEqual(first[1], "last")

    def test_a_stalled_live_request_still_yields_the_cached_fix(self):
        cached = dict(FIX, provider="gps", accuracy=15.0, elapsedMs=20 * 60 * 1000)
        with patched({
            ("gps", "last"): (True, json.dumps(cached)),
            ("gps", "once"): (False, "termux-location timed out after 25s"),
            ("network", "once"): (False, "termux-location timed out after 10s"),
        }):
            result = run()
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.data["stale"])
        self.assertTrue(result.data["live_fix_failed"])

    def test_that_answer_explains_why_the_live_fix_failed(self):
        cached = dict(FIX, provider="gps", accuracy=15.0, elapsedMs=20 * 60 * 1000)
        with patched({
            ("gps", "last"): (True, json.dumps(cached)),
            ("gps", "once"): (False, "termux-location timed out after 25s"),
        }):
            summary = run().summary
        self.assertIn("background apps", summary)
        self.assertIn("Switch to Termux", summary)
        self.assertIn("20 min old", summary)

    def test_location_being_off_is_still_reported_as_such(self):
        """Don't blame backgrounding when the switch is simply off."""
        cached = dict(FIX, provider="gps", accuracy=15.0, elapsedMs=20 * 60 * 1000)
        with patched({
            ("gps", "last"): (True, json.dumps(cached)),
            ("gps", "once"): (False, "gps provider is disabled"),
            ("network", "once"): (False, "network provider is disabled"),
        }):
            summary = run().summary
        self.assertIn("switched off", summary)
        self.assertNotIn("background apps", summary)

    def test_the_fast_path_costs_at_most_two_cached_reads(self):
        recent = dict(FIX, provider="gps", accuracy=8.0, elapsedMs=500)
        with patched({("gps", "last"): (True, json.dumps(recent))}) as fake:
            run()
        self.assertEqual(len(fake.calls), 1)

    def test_giving_up_logs_the_whole_attempt_sequence(self):
        """One line, so a failure needs no timestamp archaeology."""
        with patched({}), self.assertLogs("android_agent.tools.termux_extra", "WARNING") as logs:
            run()
        summary = "\n".join(logs.output)
        self.assertIn("get_location gave up", summary)
        self.assertIn("gps", summary)
        self.assertIn("network", summary)
        self.assertIn("precision=balanced", summary)

    def test_a_slow_cached_read_is_called_out(self):
        """A cached read is instant unless Termux:API itself is not answering."""
        def slow(fake):
            time.sleep(2.1)
            return (True, "")

        responses = {("gps", "last"): slow}
        with patched(responses), self.assertLogs(
            "android_agent.tools.termux_extra", "WARNING"
        ) as logs:
            run()
        self.assertIn("Termux:API is not answering promptly", "\n".join(logs.output))

    def test_the_failure_list_records_how_long_each_cached_read_took(self):
        with patched({}):
            result = run()
        self.assertRegex(result.summary, r"cached: .*\(\d+\.\ds\)")

    def test_location_reads_never_kill_a_timed_out_client(self):
        """Killing it is what produces the Termux:API error screen."""
        with patched({}) as fake:
            run()
        self.assertEqual(fake.kwargs.get("kill_on_timeout"), False)

    def test_gps_budget_leaves_margin_over_the_measured_fix_time(self):
        """A warm -r once fix was measured at 2.7-4.5s on the target device."""
        balanced_gps = dict(termux_extra._LOCATION_PLAN["balanced"])["gps"]
        self.assertGreaterEqual(balanced_gps, 10.0)
        self.assertLessEqual(balanced_gps, 20.0)

    def test_cached_reads_use_a_short_timeout(self):
        with patched({}) as fake:
            run()
        for provider, request, timeout in fake.calls:
            if request == "last":
                with self.subTest(provider=provider):
                    self.assertLessEqual(timeout, 10.0)


class LiveRequestCooldownTests(unittest.TestCase):
    """Stop firing live requests that are already failing.

    On a device with Android's phantom-process monitor enabled, each stalled
    termux-location call can end with the helper being SIGKILLed, which makes
    the Termux:API app throw a full-screen "Connection refused" error at the
    owner. Retrying on every message turns that into a stream of them.
    """

    STALLED: ClassVar[dict] = {"gps": (False, "termux-location timed out after 25s")}

    def _cached(self, age_minutes=30):
        return dict(FIX, provider="gps", accuracy=20.0, elapsedMs=age_minutes * 60 * 1000)

    def test_a_second_request_skips_the_live_attempt(self):
        responses = {
            ("gps", "last"): (True, json.dumps(self._cached())),
            ("gps", "once"): (False, "termux-location timed out after 25s"),
            ("network", "once"): (False, "termux-location timed out after 10s"),
        }
        with patched(responses) as fake:
            run()
            self.assertGreater(len(live_calls(fake)), 0)
            before = len(live_calls(fake))
            run()
            self.assertEqual(len(live_calls(fake)), before)

    def test_the_backed_off_answer_is_still_the_cached_fix(self):
        responses = {
            ("gps", "last"): (True, json.dumps(self._cached())),
            ("gps", "once"): (False, "termux-location timed out after 25s"),
            ("network", "once"): (False, "termux-location timed out after 10s"),
        }
        with patched(responses):
            run()
            result = run()
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.data["stale"])

    def test_a_successful_live_fix_clears_the_cooldown(self):
        stalled = {
            ("gps", "last"): (True, json.dumps(self._cached())),
            ("gps", "once"): (False, "termux-location timed out after 25s"),
            ("network", "once"): (False, "termux-location timed out after 10s"),
        }
        with patched(stalled):
            run()
        working = {("gps", "once"): (True, json.dumps(dict(FIX, accuracy=8.0)))}
        with patched(working) as fake:  # patched() resets, as a fresh process would
            run()
            self.assertIn("gps", live_calls(fake))
            run()
            self.assertEqual(live_calls(fake).count("gps"), 2)

    def test_cooldown_is_long_enough_to_matter_but_not_permanent(self):
        self.assertGreaterEqual(termux_extra._LIVE_COOLDOWN_SECONDS, 60)
        self.assertLessEqual(termux_extra._LIVE_COOLDOWN_SECONDS, 1800)

    def test_the_backoff_is_reported_in_the_failure_list(self):
        responses = {("gps", "once"): (False, "termux-location timed out after 25s")}
        with patched(responses):
            run()
            result = run()
        self.assertIn("backing off", result.summary)

    def test_phantom_process_remedy_is_named(self):
        with patched({("gps", "once"): (False, "termux-location timed out")}):
            result = run()
        self.assertIn("Disable child process restrictions", result.summary)
        self.assertIn("Developer options", result.summary)


class LocationSchemaTests(unittest.TestCase):
    def test_provider_enum_is_advertised_to_the_model(self):
        properties = spec().input_schema["properties"]
        self.assertEqual(
            set(properties["provider"]["enum"]), {"network", "gps", "passive"}
        )

    def test_description_tells_the_model_gps_comes_first(self):
        description = spec().description.lower()
        self.assertIn("gps", description)
        self.assertIn("network", description)
        self.assertIn("up to a minute", description)

    def test_location_stays_a_sensitive_read(self):
        self.assertEqual(spec().risk.value, "sensitive_read")


if __name__ == "__main__":
    unittest.main()
