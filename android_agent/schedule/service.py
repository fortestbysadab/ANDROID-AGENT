"""Background thread that ticks the scheduler.

Deliberately dumb: sleep, tick, repeat. All the judgement lives in the runner,
so this can be reasoned about on its own - the only things it must get right
are that a failing tick never kills the loop, and that stopping is prompt
rather than waiting out a full sleep.

This covers the case where the agent is running. It does **not** survive
Android killing Termux; a persisted JobScheduler job is what covers that, and
it drives the same `tick()`.
"""

from __future__ import annotations

import logging
import threading

from android_agent.schedule.runner import ScheduleRunner

logger = logging.getLogger(__name__)

#: Short enough that a task set for "in two minutes" feels prompt, long enough
#: to be invisible next to everything else the phone is doing.
DEFAULT_TICK_SECONDS = 30.0


class ScheduleService:
    def __init__(
        self,
        runner: ScheduleRunner,
        *,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        name: str = "schedule",
    ) -> None:
        if tick_seconds <= 0:
            raise ValueError("tick_seconds must be positive")
        self.runner = runner
        self.tick_seconds = tick_seconds
        self._name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
        self._thread.start()
        logger.info("Scheduler started, checking every %.0fs", self.tick_seconds)

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def tick_once(self) -> int:
        """Run anything due now. Returns how many tasks ran."""
        return len(self.runner.tick())

    def _loop(self) -> None:
        # Tick immediately on start: a task that came due while the phone was
        # off should not wait another full interval.
        while True:
            try:
                self.runner.tick()
            except Exception:
                # The runner already guards each task; this is the last line of
                # defence. A scheduler thread that dies is silent forever.
                logger.exception("Scheduler tick failed")
            if self._stop.wait(self.tick_seconds):
                logger.info("Scheduler stopped")
                return
