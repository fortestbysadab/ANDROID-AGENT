"""Last-resort position estimate from the public IP address.

This is a genuinely poor source of location and it is only ever reached when
Android itself can give nothing: every provider disabled and no cached fix.
Two properties matter and are enforced below.

**It leaves the device.** Unlike every other tool here, this makes an outbound
request to a third-party service, which necessarily learns the phone's public
IP address and that something asked where it is. `ANDROID_AGENT_IP_LOCATION=0`
switches it off entirely.

**It is wildly imprecise.** Published measurements put fixed-broadband IP
geolocation at a 3-16 km median error, and *mobile* IP at 179-207 km, because
carrier traffic egresses through a handful of central hubs. On mobile data the
answer is frequently the wrong city. The result is therefore always labelled
`approximate`, carries a deliberately pessimistic accuracy figure, and says in
words that it is not a device fix.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

#: Keyless HTTPS endpoints, tried in order.
_IP_ENDPOINTS = ("https://ipwho.is/", "https://get.geojs.io/v1/ip/geo.json")
_IP_TIMEOUT = 6.0
#: Used when the service states no radius. Mobile IP geolocation is routinely
#: ~200 km out, so anything optimistic here would be a lie.
_IP_DEFAULT_ACCURACY_M = 100_000.0
_DISABLED_VALUES = {"0", "false", "no", "off"}


def ip_location_enabled() -> bool:
    value = os.environ.get("ANDROID_AGENT_IP_LOCATION", "1").strip().lower()
    return value not in _DISABLED_VALUES


def _coerce(value: Any) -> float | None:
    """Services return coordinates as numbers or as strings; accept both."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _parse(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    # ipwho.is reports failures in-band with success=false.
    if payload.get("success") is False:
        return None
    latitude = _coerce(payload.get("latitude"))
    longitude = _coerce(payload.get("longitude"))
    if latitude is None or longitude is None:
        return None
    if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
        return None

    # GeoJS reports an accuracy radius in kilometres; treat anything else as
    # unknown rather than guessing at units.
    radius_km = _coerce(payload.get("accuracy"))
    accuracy = radius_km * 1000.0 if radius_km and radius_km > 0 else _IP_DEFAULT_ACCURACY_M

    place = ", ".join(
        str(payload[key])
        for key in ("city", "region", "country")
        if isinstance(payload.get(key), str) and payload[key]
    )
    return {
        "latitude": latitude,
        "longitude": longitude,
        "accuracy": max(accuracy, 1000.0),
        "provider": "ip",
        "approximate": True,
        "stale": False,
        "live_fix_failed": True,
        "place": place or "unknown area",
    }


def ip_location() -> dict[str, Any] | None:
    """Best-effort city-level position, or None if it cannot be had."""
    if not ip_location_enabled():
        return None
    for url in _IP_ENDPOINTS:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "android-agent"})
            with urllib.request.urlopen(request, timeout=_IP_TIMEOUT) as response:
                body = response.read(64_000)
            parsed = _parse(json.loads(body))
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
            logger.info("IP geolocation via %s failed: %s", url, type(exc).__name__)
            continue
        if parsed is not None:
            logger.warning(
                "Falling back to IP geolocation (%s): the public IP address was "
                "sent to %s because no device location was available",
                parsed["place"],
                url,
            )
            return parsed
    return None


def ip_location_summary(payload: dict[str, Any]) -> str:
    radius = payload["accuracy"] / 1000.0
    return (
        f"Approximate area only: {payload['latitude']:.4f}, {payload['longitude']:.4f} "
        f"near {payload['place']}. This came from the internet connection's IP "
        f"address, not from the device - it is accurate to no better than about "
        f"{radius:.0f} km, and on mobile data it can be in the wrong city entirely. "
        "Turn Location on for a real fix."
    )
