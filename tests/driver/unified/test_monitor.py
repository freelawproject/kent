"""Tests for the concrete ``WorkerMonitor`` (jkent.driver.unified_driver).

Binds the real implementation to the reusable ``MonitorConformance`` suite,
then adds targeted ``run()``-loop tests with fast, pre-arranged conditions.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jkent.driver.unified_driver import Monitor
from jkent.driver.unified_driver.orchestration import Run, WorkerMonitor
from tests.driver.unified.test_monitor_conformance import MonitorConformance

if TYPE_CHECKING:
    from jkent.driver.unified_driver.transport import Transport


class _FakeRun(Run):
    """A run exposing a mutable ``active_worker_count`` and ``spawn_worker``.

    Subclasses the ``Run`` ABC (so it types as ``WorkerMonitor``'s ``run``
    argument), but only ``active_worker_count`` and ``spawn_worker`` are
    exercised — the monitor never touches the lifecycle members, which are
    inert stubs here. ``active_worker_count`` is a property (Run declares it
    one), backed by ``_active``.
    """

    def __init__(self, active_worker_count: int = 0) -> None:
        self._active = active_worker_count
        self.spawn_calls = 0

    @property
    def active_worker_count(self) -> int:
        return self._active

    def spawn_worker(self) -> int:
        self.spawn_calls += 1
        self._active += 1
        return self._active

    # --- inert Run lifecycle: present for typing, unused by the monitor ---

    @property
    def transport(self) -> Transport:  # pragma: no cover
        raise NotImplementedError

    async def open(self) -> None:  # pragma: no cover
        ...

    async def aclose(self) -> None:  # pragma: no cover
        ...

    async def run(self) -> None:  # pragma: no cover
        ...

    def stop(self) -> None:  # pragma: no cover
        ...

    async def status(  # pragma: no cover
        self,
    ) -> Literal["unstarted", "in_progress", "done"]:
        return "unstarted"


class _FakeRateLimiter:
    """A rate limiter exposing only ``max_rate_per_second``."""

    def __init__(self, max_rate_per_second: float | None) -> None:
        self._max_rate_per_second = max_rate_per_second

    async def gate(self, request: object) -> None:  # pragma: no cover
        return None

    @property
    def max_rate_per_second(self) -> float | None:
        return self._max_rate_per_second


def _pending(n: int) -> Callable[[], Awaitable[int]]:
    """A ``pending_requests`` callable that always reports ``n``."""

    async def pending() -> int:
        return n

    return pending


# --- conformance suite bound to the real implementation ------------------


class TestWorkerMonitor(MonitorConformance):
    """Run the conformance suite against the real ``WorkerMonitor``."""

    def make_monitor(
        self,
        *,
        max_workers: int,
        max_rate_per_second: float | None,
        active_worker_count: int = 0,
        durations: Iterable[float] = (),
        window: int = 100,
    ) -> Monitor:
        monitor = WorkerMonitor(
            _FakeRun(active_worker_count=active_worker_count),
            _FakeRateLimiter(max_rate_per_second),
            max_workers=max_workers,
            pending_requests=_pending(0),
            window=window,
        )
        for d in durations:
            monitor.record_request_duration(d)
        return monitor

    @pytest.fixture
    def subject(self) -> Monitor:
        return WorkerMonitor(
            _FakeRun(),
            _FakeRateLimiter(2.0),
            max_workers=8,
            pending_requests=_pending(0),
        )

    # The inherited ``@given`` methods are re-declared here so this binding
    # owns its own function objects; sharing them with the reference suite's
    # subclass trips hypothesis's ``differing_executors`` health check.

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


# --- targeted run()-loop tests -------------------------------------------


async def test_run_exits_when_stopped() -> None:
    """The loop returns promptly when the stop event is pre-set."""
    run = _FakeRun(active_worker_count=2)
    monitor = WorkerMonitor(
        run,
        _FakeRateLimiter(2.0),
        max_workers=8,
        pending_requests=_pending(10),
        poll_interval=0.001,
    )
    monitor.stop_event.set()
    await asyncio.wait_for(monitor.run(), timeout=1.0)
    assert run.spawn_calls == 0


async def test_run_exits_when_idle_and_no_pending() -> None:
    """The loop exits once active == 0 and there is no pending work."""
    run = _FakeRun(active_worker_count=0)
    monitor = WorkerMonitor(
        run,
        _FakeRateLimiter(2.0),
        max_workers=8,
        pending_requests=_pending(0),
        poll_interval=0.001,
    )
    await asyncio.wait_for(monitor.run(), timeout=1.0)
    assert run.spawn_calls == 0


async def test_run_scales_up_under_load() -> None:
    """With pending work, low active count, and timing data, it spawns."""
    run = _FakeRun(active_worker_count=1)
    # 4 req/s * 1.0 s avg -> workers_needed = 4, capped at max_workers=3.
    monitor = WorkerMonitor(
        run,
        _FakeRateLimiter(4.0),
        max_workers=3,
        pending_requests=_pending(10),
        poll_interval=0.001,
    )
    monitor.record_request_duration(1.0)

    async def stop_soon() -> None:
        # Let the loop run a few cycles, then stop it.
        await asyncio.sleep(0.05)
        monitor.stop_event.set()

    await asyncio.gather(
        asyncio.wait_for(monitor.run(), timeout=2.0), stop_soon()
    )
    # Deterministic: starts at 1 and spawns up to the max_workers=3 ceiling.
    assert run.spawn_calls == 2
    assert run.active_worker_count == 3


async def test_run_scales_only_to_workers_needed_below_max() -> None:
    """Scaling stops at ``workers_needed()`` when it is below ``max_workers``.

    The rate-based target is the binding ceiling here, not ``max_workers`` —
    so a loop that ignored ``workers_needed()`` and scaled straight to the
    pool maximum would over-spawn and fail this test.
    """
    run = _FakeRun(active_worker_count=1)
    # 2 req/s * 1.0 s avg -> workers_needed = 2, well below max_workers=8.
    monitor = WorkerMonitor(
        run,
        _FakeRateLimiter(2.0),
        max_workers=8,
        pending_requests=_pending(10),
        poll_interval=0.001,
    )
    monitor.record_request_duration(1.0)

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        monitor.stop_event.set()

    await asyncio.gather(
        asyncio.wait_for(monitor.run(), timeout=2.0), stop_soon()
    )
    # Starts at 1, spawns once to reach the rate-based target of 2, no more.
    assert run.spawn_calls == 1
    assert run.active_worker_count == 2


async def test_run_does_not_spawn_without_pending_work() -> None:
    """No spawn when there is no pending work but workers are still active."""
    run = _FakeRun(active_worker_count=1)
    monitor = WorkerMonitor(
        run,
        _FakeRateLimiter(4.0),
        max_workers=8,
        pending_requests=_pending(0),
        poll_interval=0.001,
    )
    monitor.record_request_duration(1.0)

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        monitor.stop_event.set()

    await asyncio.gather(
        asyncio.wait_for(monitor.run(), timeout=2.0), stop_soon()
    )
    assert run.spawn_calls == 0


async def test_run_requires_two_idle_cycles_to_exit() -> None:
    """A single idle read does not stop the loop; it takes two in a row.

    Pins the debounce: ``pending`` is polled once per cycle, so an exit after
    exactly two reads proves the loop did not quit on the first idle read.
    """
    run = _FakeRun(active_worker_count=0)
    reads = 0

    async def pending() -> int:
        nonlocal reads
        reads += 1
        return 0

    monitor = WorkerMonitor(
        run,
        _FakeRateLimiter(2.0),
        max_workers=8,
        pending_requests=pending,
        poll_interval=0.001,
    )
    await asyncio.wait_for(monitor.run(), timeout=1.0)
    assert reads == 2
    assert run.spawn_calls == 0


async def test_run_resumes_scaling_after_transient_idle() -> None:
    """A lone idle read is debounced; scaling resumes once work reappears.

    The first poll reports no work (a transient lull with no active workers),
    which alone must not end the run. Work then reappears and the monitor
    must still scale — exactly the case the two-cycle debounce exists for.
    """
    run = _FakeRun(active_worker_count=0)
    reads = 0

    async def pending() -> int:
        nonlocal reads
        reads += 1
        # First read: transient lull. Subsequent reads: work is waiting.
        return 0 if reads == 1 else 10

    monitor = WorkerMonitor(
        run,
        _FakeRateLimiter(4.0),
        max_workers=3,
        pending_requests=pending,
        poll_interval=0.001,
    )
    monitor.record_request_duration(1.0)

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        monitor.stop_event.set()

    await asyncio.gather(
        asyncio.wait_for(monitor.run(), timeout=2.0), stop_soon()
    )
    assert run.spawn_calls >= 1
