"""Tests for the IP-address location fallback.

This is the only tool in the project that talks to a third party, and the only
one that can return a position the device never measured. The tests below pin
down the two things that make that acceptable: it is switchable, and it never
disguises itself as a real fix.
"""

from __future__ import annotations

import json
import unittest
import urllib.error
from unittest import mock

from android_agent.tools import ip_location as ipmod
from android_agent.tools import termux_extra
from android_agent.tools.ip_location import (
    _IP_DEFAULT_ACCURACY_M,
    ip_location,
    ip_location_enabled,
    ip_location_summary,
)

IPWHOIS = {
    "ip": "203.0.113.7", "success": True, "country": "India",
    "region": "West Bengal", "city": "Durgapur",
    "latitude": 23.5204, "longitude": 87.3119,
}
GEOJS = {
    "ip": "203.0.113.7", "city": "Durgapur", "region": "West Bengal",
    "country": "India", "latitude": "23.5204", "longitude": "87.3119",
    "accuracy": 20,
}


class FakeHttp:
    """Maps URL -> body bytes, or an exception to raise."""

    def __init__(self, responses):
        self.responses = responses
        self.urls: list[str] = []

    def __call__(self, request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        self.urls.append(url)
        reply = self.responses.get(url)
        if reply is None:
            raise urllib.error.URLError("unreachable")
        if isinstance(reply, Exception):
            raise reply
        return mock.MagicMock(
            __enter__=lambda s: mock.MagicMock(read=lambda *a: reply),
            __exit__=lambda *a: False,
        )


def http(responses):
    return mock.patch.object(ipmod.urllib.request, "urlopen", FakeHttp(responses))


def body(payload):
    return json.dumps(payload).encode()


class ToggleTests(unittest.TestCase):
    def test_enabled_by_default(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("ANDROID_AGENT_IP_LOCATION", None)
            self.assertTrue(ip_location_enabled())

    def test_can_be_switched_off(self):
        for value in ("0", "false", "no", "off", "OFF", " 0 "):
            with self.subTest(value=value), mock.patch.dict(
                "os.environ", {"ANDROID_AGENT_IP_LOCATION": value}
            ):
                self.assertFalse(ip_location_enabled())

    def test_disabled_means_no_request_is_made(self):
        with mock.patch.dict("os.environ", {"ANDROID_AGENT_IP_LOCATION": "0"}), http(
            {"https://ipwho.is/": body(IPWHOIS)}
        ) as fake:
            self.assertIsNone(ip_location())
        self.assertEqual(fake.urls, [])


class LookupTests(unittest.TestCase):
    def test_primary_service_is_parsed(self):
        with http({"https://ipwho.is/": body(IPWHOIS)}):
            result = ip_location()
        self.assertAlmostEqual(result["latitude"], 23.5204)
        self.assertEqual(result["provider"], "ip")
        self.assertEqual(result["place"], "Durgapur, West Bengal, India")

    def test_falls_through_to_the_second_service(self):
        with http({"https://get.geojs.io/v1/ip/geo.json": body(GEOJS)}) as fake:
            result = ip_location()
        self.assertIsNotNone(result)
        self.assertEqual(len(fake.urls), 2)

    def test_string_coordinates_are_accepted(self):
        with http({"https://get.geojs.io/v1/ip/geo.json": body(GEOJS)}):
            result = ip_location()
        self.assertAlmostEqual(result["longitude"], 87.3119)

    def test_kilometre_radius_is_converted_to_metres(self):
        with http({"https://get.geojs.io/v1/ip/geo.json": body(GEOJS)}):
            result = ip_location()
        self.assertEqual(result["accuracy"], 20_000.0)

    def test_missing_radius_falls_back_to_a_pessimistic_figure(self):
        with http({"https://ipwho.is/": body(IPWHOIS)}):
            result = ip_location()
        self.assertEqual(result["accuracy"], _IP_DEFAULT_ACCURACY_M)

    def test_pessimistic_figure_is_honest_about_mobile_ip_error(self):
        """Mobile IP geolocation is ~200 km out; a small number would lie."""
        self.assertGreaterEqual(_IP_DEFAULT_ACCURACY_M, 50_000.0)

    def test_in_band_failure_is_not_treated_as_a_fix(self):
        with http({"https://ipwho.is/": body({"success": False, "message": "quota"})}):
            self.assertIsNone(ip_location())

    def test_out_of_range_coordinates_are_rejected(self):
        with http({"https://ipwho.is/": body(dict(IPWHOIS, latitude=999))}):
            self.assertIsNone(ip_location())

    def test_garbage_body_is_survivable(self):
        with http({"https://ipwho.is/": b"<html>nope</html>"}):
            self.assertIsNone(ip_location())

    def test_total_failure_returns_none(self):
        with http({}):
            self.assertIsNone(ip_location())


class HonestyTests(unittest.TestCase):
    def setUp(self):
        with http({"https://ipwho.is/": body(IPWHOIS)}):
            self.result = ip_location()

    def test_result_is_flagged_approximate(self):
        self.assertTrue(self.result["approximate"])
        self.assertTrue(self.result["live_fix_failed"])
        self.assertEqual(self.result["provider"], "ip")

    def test_summary_says_it_is_not_a_device_fix(self):
        summary = ip_location_summary(self.result)
        self.assertIn("not from the device", summary)
        self.assertIn("IP", summary)
        self.assertIn("Durgapur", summary)

    def test_summary_states_the_error_scale_and_the_remedy(self):
        summary = ip_location_summary(self.result)
        self.assertIn("100 km", summary)
        self.assertIn("wrong city", summary)
        self.assertIn("Turn Location on", summary)


class IntegrationTests(unittest.TestCase):
    """get_location must exhaust the device before ever going to the network."""

    def _run(self, responses, ip):
        fake = mock.Mock(return_value=ip)
        with mock.patch.object(termux_extra, "_run", lambda *a, **k: responses), \
             mock.patch.object(termux_extra, "ip_location", fake):
            return termux_extra._location(None, {}), fake

    def test_ip_fallback_is_used_when_the_device_gives_nothing(self):
        estimate = {
            "latitude": 23.5, "longitude": 87.3, "accuracy": 100_000.0,
            "provider": "ip", "approximate": True, "stale": False,
            "live_fix_failed": True, "place": "Durgapur, India",
        }
        result, fake = self._run((True, ""), estimate)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["provider"], "ip")
        self.assertIn("not from the device", result.summary)
        fake.assert_called_once()

    def test_a_real_fix_never_triggers_a_network_call(self):
        fix = json.dumps({"latitude": 22.36, "longitude": 88.0, "accuracy": 8.0,
                          "provider": "gps", "elapsedMs": 100})
        result, fake = self._run((True, fix), {"latitude": 0, "longitude": 0})
        self.assertEqual(result.data["provider"], "gps")
        fake.assert_not_called()

    def test_error_is_still_returned_when_the_ip_lookup_also_fails(self):
        result, _ = self._run((True, ""), None)
        self.assertEqual(result.status, "error")


if __name__ == "__main__":
    unittest.main()
