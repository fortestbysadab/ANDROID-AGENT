#!/usr/bin/env python3
"""Compare every way of calling termux-location, back to back.

Why this exists: `termux-location -p gps -r once` was observed returning a fix
in five seconds when run from an interactive shell, and timing out after
twenty-five seconds when run by the agent fourteen minutes later. Two
explanations fit that equally well:

  A. The *spawn style* matters - the agent uses pipes, a closed stdin and a
     new session (setsid), the shell does not.
  B. Nothing about the agent matters and the GPS had simply gone cold between
     the two attempts, which is normal indoors.

Reasoning from one sample of each cannot separate those. This runs every
variant within the same minute, so warm/cold is held constant and spawn style
is the only thing that differs. Run it twice - once with Termux on screen and
once with Termux in the background - to separate foreground from background.

    python scripts/probe_location.py
    python scripts/probe_location.py --json    # machine-readable

Nothing here is used at runtime; it is a diagnostic.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

TIMEOUT = 30.0


def _describe(raw: bytes | None) -> str:
    text = (raw or b"").decode("utf-8", "replace").strip()
    if not text:
        return "EMPTY OUTPUT (exit 0 with nothing - a real Termux:API failure mode)"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return f"UNPARSEABLE: {text[:80]}"
    if not isinstance(data, dict) or "latitude" not in data:
        return f"NO COORDINATES: {text[:80]}"
    age = data.get("elapsedMs")
    age_text = f", {float(age) / 1000:.1f}s old" if isinstance(age, (int, float)) else ""
    return (
        f"FIX {data['latitude']:.5f},{data['longitude']:.5f} "
        f"(+/-{data.get('accuracy', '?')}m, {data.get('provider', '?')}{age_text})"
    )


def attempt(label: str, args: list[str], *, new_session: bool, pipes: bool) -> dict:
    """Run one variant and time it."""
    started = time.monotonic()
    kwargs: dict = {"start_new_session": new_session}
    if pipes:
        kwargs.update(
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    else:
        # As close to a plain shell invocation as possible: inherited stdin,
        # output captured only because we have to read it somehow.
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        process = subprocess.Popen(args, **kwargs)
    except FileNotFoundError:
        return {"label": label, "seconds": 0.0, "outcome": "termux-location not installed"}

    try:
        out, err = process.communicate(timeout=TIMEOUT)
        outcome = _describe(out)
        if process.returncode != 0:
            detail = (err or b"").decode("utf-8", "replace").strip()[:80]
            outcome = f"EXIT {process.returncode}: {detail or outcome}"
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        outcome = f"TIMED OUT after {TIMEOUT:.0f}s"

    return {"label": label, "seconds": round(time.monotonic() - started, 1), "outcome": outcome}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    options = parser.parse_args()

    gps_last = ["termux-location", "-p", "gps", "-r", "last"]
    gps_once = ["termux-location", "-p", "gps", "-r", "once"]
    net_once = ["termux-location", "-p", "network", "-r", "once"]

    plan = [
        # The cheap read the agent tries first.
        ("gps -r last, shell style", gps_last, False, False),
        ("gps -r last, agent style", gps_last, True, True),
        # The expensive read that was seen timing out.
        ("gps -r once, shell style", gps_once, False, False),
        ("gps -r once, agent style", gps_once, True, True),
        # Isolate the two differences in the agent's spawn.
        ("gps -r once, pipes only", gps_once, False, True),
        ("gps -r once, setsid only", gps_once, True, False),
        ("network -r once, agent style", net_once, True, True),
    ]

    results = [
        attempt(label, args, new_session=session, pipes=pipes)
        for label, args, session, pipes in plan
    ]

    if options.json:
        print(json.dumps(results, indent=2))
        return 0

    width = max(len(row["label"]) for row in results)
    print("\nEach line ran immediately after the previous one.\n")
    for row in results:
        print(f"{row['label']:<{width}}  {row['seconds']:>5.1f}s  {row['outcome']}")

    shell = next(r for r in results if r["label"] == "gps -r once, shell style")
    agent = next(r for r in results if r["label"] == "gps -r once, agent style")
    print("\nReading this:")
    if "TIMED OUT" in shell["outcome"] and "TIMED OUT" in agent["outcome"]:
        print("  Both timed out - the GPS itself is not fixing. Not a spawn-style")
        print("  problem. Go outdoors, or rely on the cached '-r last' reading.")
    elif "TIMED OUT" in agent["outcome"] and "FIX" in shell["outcome"]:
        print("  The agent's spawn style IS the difference. Compare the")
        print("  'pipes only' and 'setsid only' rows to see which one causes it.")
    elif "FIX" in agent["outcome"]:
        print("  The agent's spawn style is fine. An earlier failure was most")
        print("  likely a cold GPS at that moment, not a code problem.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
