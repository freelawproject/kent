"""Reusable conformance suite for ``Monitor`` (jkent.driver.unified_driver).

The monitor right-sizes the worker pool from an *in-memory* window of recent
request durations fed by the workers — a scaling cycle issues no DB query.

Contract under test (see ``monitor_contract.md``):

- ABC conformance: the subject is a ``Monitor`` instance (subclasses the ABC).
- ``recent_avg_request_duration_s()`` is ``None`` until a duration is
  recorded, and equals the mean of the window thereafter.
- ``record_request_duration`` feeds a bounded in-memory window; the average
  reflects what has been fed (and is reused by ``workers_needed``).
- ``workers_needed()`` is
  ``ceil(max_rate_per_second * recent_avg_request_duration_s)`` clamped to
  ``[1, max_workers]``.
- Unlimited rate (``max_rate_per_second is None``) targets ``max_workers``.
- No timing data yet is conservative: one above the current active count
  (clamped to ``[1, max_workers]``).
- The math is derived only from in-memory state (window + configured rate),
  never a database.

``MonitorConformance`` is the reusable base (no ``Test`` prefix). Subclasses
override ``make_monitor`` / ``subject`` to bind an implementation. A reference
fake monitor and ``TestReferenceMonitor`` exercise the suite in this file.
"""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import Iterable

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jkent.driver.unified_driver import Monitor

# --- Reference fake monitor ----------------------------------------------


class FakeMonitor(Monitor):
    """Minimal in-memory ``Monitor`` implementing the contract's math.

    Sizing reads only the duration window and the configured rate — no DB.
    """

    def __init__(
        self,
        *,
        max_workers: int,
        max_rate_per_second: float | None,
        active_worker_count: int = 0,
        window: int = 100,
        durations: Iterable[float] = (),
    ) -> None:
        self.max_workers = max_workers
        self.max_rate_per_second = max_rate_per_second
        self.active_worker_count = active_worker_count
        self._durations: deque[float] = deque(maxlen=window)
        self._stop_event = asyncio.Event()
        for d in durations:
            self.record_request_duration(d)

    async def run(self) -> None:
        """Block until the stop flag is set; return promptly once it is."""
        await self._stop_event.wait()

    def stop(self) -> None:
        """Set the stop flag so a running ``run`` returns."""
        self._stop_event.set()

    def record_request_duration(self, duration_s: float) -> None:
        """Append one completed request's duration to the in-memory window."""
        self._durations.append(duration_s)

    def recent_avg_request_duration_s(self) -> float | None:
        """Mean of the window, or ``None`` until a duration is recorded."""
        if not self._durations:
            return None
        return sum(self._durations) / len(self._durations)

    def workers_needed(self) -> int:
        """Target pool size from rate headroom and recent avg duration."""
        avg = self.recent_avg_request_duration_s()
        if avg is None:
            # Conservative: one above current, clamped to the pool bounds.
            target = self.active_worker_count + 1
        elif self.max_rate_per_second is None:
            target = self.max_workers
        else:
            target = math.ceil(self.max_rate_per_second * avg)
        return max(1, min(target, self.max_workers))


# --- Reusable conformance base -------------------------------------------


class MonitorConformance:
    """Contract tests every ``Monitor`` implementation must satisfy.

    Subclasses override :meth:`make_monitor` (and optionally :attr:`subject`)
    to bind a concrete implementation.
    """

    def make_monitor(
        self,
        *,
        max_workers: int,
        max_rate_per_second: float | None,
        active_worker_count: int = 0,
        durations: Iterable[float] = (),
        window: int = 100,
    ) -> Monitor:
        """Construct a ``Monitor`` with the given configuration."""
        raise NotImplementedError

    @pytest.fixture
    def subject(self) -> Monitor:
        """A default-configured ``Monitor`` instance for ABC checks."""
        raise NotImplementedError

    # --- ABC conformance -------------------------------------------------

    def test_is_a_monitor(self, subject: Monitor) -> None:
        assert isinstance(subject, Monitor)

    # --- duration window + mean ------------------------------------------

    def test_avg_is_none_before_any_data(self) -> None:
        monitor = self.make_monitor(max_workers=8, max_rate_per_second=2.0)
        assert monitor.recent_avg_request_duration_s() is None

    def test_avg_equals_mean_after_feeding(self) -> None:
        monitor = self.make_monitor(max_workers=8, max_rate_per_second=2.0)
        for d in (1.0, 2.0, 3.0):
            monitor.record_request_duration(d)
        avg = monitor.recent_avg_request_duration_s()
        assert avg == pytest.approx(2.0)

    @pytest.mark.generative
    @given(
        durations=st.lists(st.floats(0.001, 100.0), min_size=1, max_size=50)
    )
    def test_avg_is_window_mean(self, durations: list[float]) -> None:
        monitor = self.make_monitor(max_workers=8, max_rate_per_second=2.0)
        for d in durations:
            monitor.record_request_duration(d)
        avg = monitor.recent_avg_request_duration_s()
        assert avg is not None
        assert avg == pytest.approx(sum(durations) / len(durations))

    def test_window_is_bounded_to_recent(self) -> None:
        # A bounded window keeps only the most recent ``window`` durations:
        # readings older than that are evicted and stop affecting the mean.
        # An unbounded implementation would average all six -> 25.5.
        monitor = self.make_monitor(
            max_workers=8, max_rate_per_second=2.0, window=3
        )
        for d in (50.0, 50.0, 50.0, 1.0, 1.0, 1.0):
            monitor.record_request_duration(d)
        assert monitor.recent_avg_request_duration_s() == pytest.approx(1.0)

    # --- workers_needed formula + clamping -------------------------------

    def test_no_timing_data_is_conservative(self) -> None:
        # One above current active count, clamped into bounds.
        monitor = self.make_monitor(
            max_workers=8, max_rate_per_second=2.0, active_worker_count=3
        )
        assert monitor.recent_avg_request_duration_s() is None
        assert monitor.workers_needed() == 4

    def test_no_timing_data_clamps_to_max(self) -> None:
        monitor = self.make_monitor(
            max_workers=4, max_rate_per_second=2.0, active_worker_count=4
        )
        assert monitor.workers_needed() == 4

    def test_unlimited_rate_targets_max_workers(self) -> None:
        monitor = self.make_monitor(
            max_workers=6, max_rate_per_second=None, durations=(0.5,)
        )
        assert monitor.workers_needed() == 6

    def test_workers_needed_follows_formula(self) -> None:
        # 4 req/s * 1.5 s avg = 6.0 -> ceil -> 6, within [1, 16].
        monitor = self.make_monitor(
            max_workers=16, max_rate_per_second=4.0, durations=(1.5,)
        )
        assert monitor.workers_needed() == 6

    def test_workers_needed_clamps_low_to_one(self) -> None:
        # 1 req/s * 0.1 s avg = 0.1 -> ceil -> 1, floor of the clamp.
        monitor = self.make_monitor(
            max_workers=8, max_rate_per_second=1.0, durations=(0.1,)
        )
        assert monitor.workers_needed() == 1

    def test_workers_needed_clamps_high_to_max(self) -> None:
        # 100 req/s * 2 s avg = 200 -> clamped down to max_workers.
        monitor = self.make_monitor(
            max_workers=5, max_rate_per_second=100.0, durations=(2.0,)
        )
        assert monitor.workers_needed() == 5

    @pytest.mark.generative
    @given(
        max_workers=st.integers(min_value=1, max_value=64),
        rate=st.floats(min_value=0.01, max_value=1_000.0),
        durations=st.lists(st.floats(0.001, 100.0), min_size=1, max_size=30),
    )
    def test_workers_needed_formula_and_clamp(
        self, max_workers: int, rate: float, durations: list[float]
    ) -> None:
        monitor = self.make_monitor(
            max_workers=max_workers,
            max_rate_per_second=rate,
            durations=durations,
        )
        avg = sum(durations) / len(durations)
        expected = max(1, min(math.ceil(rate * avg), max_workers))
        assert monitor.workers_needed() == expected
        assert 1 <= monitor.workers_needed() <= max_workers

    @pytest.mark.generative
    @given(
        max_workers=st.integers(min_value=1, max_value=64),
        durations=st.lists(st.floats(0.001, 100.0), min_size=1, max_size=30),
    )
    def test_unlimited_rate_always_max(
        self, max_workers: int, durations: list[float]
    ) -> None:
        monitor = self.make_monitor(
            max_workers=max_workers,
            max_rate_per_second=None,
            durations=durations,
        )
        assert monitor.workers_needed() == max_workers

    @pytest.mark.generative
    @given(
        max_workers=st.integers(min_value=1, max_value=64),
        active=st.integers(min_value=0, max_value=128),
    )
    def test_no_data_conservative_clamped(
        self, max_workers: int, active: int
    ) -> None:
        monitor = self.make_monitor(
            max_workers=max_workers,
            max_rate_per_second=2.0,
            active_worker_count=active,
        )
        assert monitor.recent_avg_request_duration_s() is None
        expected = max(1, min(active + 1, max_workers))
        assert monitor.workers_needed() == expected


# --- Reference implementation bound to the suite -------------------------


class TestReferenceMonitor(MonitorConformance):
    """Run the conformance suite against the in-file ``FakeMonitor``."""

    def make_monitor(
        self,
        *,
        max_workers: int,
        max_rate_per_second: float | None,
        active_worker_count: int = 0,
        durations: Iterable[float] = (),
        window: int = 100,
    ) -> Monitor:
        return FakeMonitor(
            max_workers=max_workers,
            max_rate_per_second=max_rate_per_second,
            active_worker_count=active_worker_count,
            durations=durations,
            window=window,
        )

    @pytest.fixture
    def subject(self) -> Monitor:
        return FakeMonitor(max_workers=8, max_rate_per_second=2.0)

    async def test_run_exits_when_stopped(self) -> None:
        """``run`` returns once the stop flag is set."""
        monitor = FakeMonitor(max_workers=8, max_rate_per_second=2.0)
        monitor.stop()
        await asyncio.wait_for(monitor.run(), timeout=1.0)

    async def test_run_blocks_until_stopped(self) -> None:
        """``run`` does not return on its own while unstopped.

        Pairs with :meth:`test_run_exits_when_stopped`: together they show the
        return is *caused* by ``stop()``, not by ``run`` exiting regardless.
        """
        monitor = FakeMonitor(max_workers=8, max_rate_per_second=2.0)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(monitor.run(), timeout=0.05)
