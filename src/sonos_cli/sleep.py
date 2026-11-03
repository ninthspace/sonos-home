"""Keep the Mac awake while a room plays audio this service serves (FR17).

Playback happens on the speaker, so macOS sees no activity. While any group's coordinator is PLAYING one of
our URLs, or has been PAUSED on one for less than the grace period, one `caffeinate -i -w <service pid>` runs.
It is released as soon as nothing of ours qualifies (well inside 30 s: the monitor polls rooms playing ours
every 2 s, and `check()` runs every few seconds for the paused-too-long case).
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Callable

HOLDING_STATES = {"PLAYING", "TRANSITIONING"}
PAUSED = "PAUSED_PLAYBACK"


class SleepAssertion:
    def __init__(self, monitor, *, grace_minutes: float = 30.0, clock: Callable[[], float] = time.monotonic,
                 spawn: Callable[[list[str]], object] | None = None, pid: int | None = None,
                 check_every: float = 5.0):
        self.monitor = monitor
        self.grace = grace_minutes * 60
        self.clock = clock
        self.spawn = spawn or (lambda cmd: subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        self.pid = pid or os.getpid()
        self.check_every = check_every
        self._proc = None
        self._paused_since: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        monitor.subscribe(self._on_change)

    # ---- what qualifies --------------------------------------------------------------------------------
    def _qualifies(self, coordinator_uid: str, st: dict | None, now: float) -> bool:
        if not st or st.get("source") != "ours" or st.get("reachable") is False:
            return False        # a speaker that stopped answering is not known to be playing
        state = st.get("state")
        if state in HOLDING_STATES:
            self._paused_since.pop(coordinator_uid, None)
            return True
        if state == PAUSED:
            since = self._paused_since.setdefault(coordinator_uid, now)
            return now - since <= self.grace
        self._paused_since.pop(coordinator_uid, None)
        return False

    def _on_change(self, coordinator_uid: str, old, new) -> None:
        self.check()

    def check(self) -> bool:
        """Hold or release according to the monitor's cache; returns whether the assertion is held."""
        now = self.clock()
        with self._lock:
            if self._stop.is_set():                  # shutting down: never start a new caffeinate
                return False
            wanted = False
            for uid, st in self.monitor.group_states().items():
                if self._qualifies(uid, st, now):
                    wanted = True
            if wanted:
                if self._proc is None or self._proc.poll() is not None:      # also restarts a caffeinate that died
                    self._proc = self.spawn(["caffeinate", "-i", "-w", str(self.pid)])
            elif self._proc is not None:
                self._release()
            return self._proc is not None

    def _release(self) -> None:
        proc, self._proc = self._proc, None
        try:
            proc.terminate()
        except Exception:
            pass

    @property
    def held(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    # ---- the periodic check (a pause can outlast the grace period without any state change) ----------------
    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sleep-assertion", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(5)
            self._thread = None
        with self._lock:
            if self._proc is not None:
                self._release()

    def _run(self) -> None:
        while not self._stop.wait(self.check_every):
            try:
                self.check()
            except Exception:
                pass
