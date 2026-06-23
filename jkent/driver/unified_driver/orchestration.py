"""Orchestration roles: the parts that drive a scrape and stay transport-blind.

Where :class:`~jkent.driver.unified_driver.transport.Transport` owns *how* a
request runs, these own *when and how many*. They never touch a browser or
an HTTP client — they pull work, size the worker pool, compact storage, and
own the run's lifecycle, delegating every actual fetch to the transport.
Splitting them out keeps each responsibility inspectable on its own.
"""

from __future__ import annotations

import abc
import asyncio
import math
from collections import deque
from typing import TYPE_CHECKING, Literal

from jkent.driver.unified_driver.compression import (
    DEFAULT_DICT_SIZE,
    get_compression_dict,
    recompress_responses,
    train_compression_dict,
)
from jkent.driver.unified_driver.lifecycle import AsyncLifecycle

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from jkent.driver.unified_driver.rate_limiter import RateLimiter
    from jkent.driver.unified_driver.transport import Transport


class Worker(abc.ABC):
    """A single unit of execution within a run.

    Leases a per-worker handle from the transport, then loops: pull the
    next request from the queue, resolve it via the transport, persist the
    result, and route failures by the exception taxonomy (transient →
    retry, persistent → fail, halt/skip → propagate). Exits on graceful
    shutdown or sustained idle, releasing its handle on the way out.

    A worker observes transport failure at point of use — it is the one
    holding the handle when the resource dies — and recovers by asking the
    transport to restart, then renewing its lease (see
    :class:`~jkent.driver.unified_driver.lifecycle.Recoverable`).
    """

    worker_id: int

    @abc.abstractmethod
    async def run(self) -> None:
        """Process requests until shutdown or the queue is durably empty."""


class Monitor(abc.ABC):
    """Observer that right-sizes the worker pool; never executes requests.

    Runs a periodic loop that reads load — pending work, rate-limit
    headroom, recent request durations — and asks the run to spawn workers
    up to its ceiling. It only ever scales *up*; workers retire themselves
    on idle. Sizing reads from an in-memory window of recent request
    durations fed by the workers, so a scaling cycle issues no database
    query. Storage upkeep (compression-dict training) is not this role's
    concern — see :class:`Compactor`.
    """

    @abc.abstractmethod
    async def run(self) -> None:
        """Loop until shutdown, sizing the pool each cycle."""

    @abc.abstractmethod
    def record_request_duration(self, duration_s: float) -> None:
        """Feed one completed request's wall-clock duration into the window.

        Workers report here as each request finishes; the monitor sizes the
        pool from this in-memory window rather than a per-cycle query for
        average request duration.
        """

    @abc.abstractmethod
    def recent_avg_request_duration_s(self) -> float | None:
        """Mean of the recent-duration window, or None until data exists."""

    @abc.abstractmethod
    def workers_needed(self) -> int:
        """Target worker count: rate-limit headroom ÷ recent avg duration."""


class Run(AsyncLifecycle):
    """The outermost lifecycle: owner and supervisor of a single scrape.

    Holds the transport, the worker registry, and the monitor, and ties
    their lifetimes to the run. ``open``/``aclose`` (from
    :class:`~jkent.driver.unified_driver.lifecycle.AsyncLifecycle`) bring the
    transport and queue up and down; ``run`` drives the scrape to
    completion; ``stop`` requests a graceful, resumable shutdown.

    The transport is a peer the run owns for the whole scrape — it survives
    individual worker exits and is rebuilt in place on crash, never torn
    down by the run mid-scrape.
    """

    @property
    @abc.abstractmethod
    def transport(self) -> Transport:
        """The request-execution backend for this run."""

    @property
    @abc.abstractmethod
    def active_worker_count(self) -> int:
        """Number of workers currently running."""

    @abc.abstractmethod
    def spawn_worker(self) -> int:
        """Start a worker and return its id (called by the run and monitor)."""

    @abc.abstractmethod
    async def run(self) -> None:
        """Start the workers and monitor and drive the scrape to completion."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Signal graceful shutdown: in-flight requests finish, then workers exit."""

    @abc.abstractmethod
    async def status(self) -> Literal["unstarted", "in_progress", "done"]:
        """Current run state, derived from queue and worker activity."""


class Compactor:
    """Per-step compaction — counts in memory, trains once at the threshold.

    One Compactor is created for each scraper step that currently has fewer
    than ``threshold`` stored responses. The "no other work" is specifically
    *no DB polling*: it tracks the step's response count in memory (bumped by
    the run as each request for the step completes) instead of querying the
    database to decide when to act. On the call that reaches ``threshold`` it
    **owns** the one-shot job — train a zstd dictionary for the step from its
    stored responses and recompress them — then goes inert for the rest of the
    run.

    Replaces the periodic compression pass the worker monitor used to run.
    """

    THRESHOLD = 1000

    def __init__(
        self,
        step: str,
        session_factory: async_sessionmaker,
        *,
        db_lock: asyncio.Lock | None = None,
        threshold: int = THRESHOLD,
        count: int = 0,
        sample_limit: int | None = None,
        dict_size: int | None = None,
    ) -> None:
        self.step = step
        self.count = count
        self.threshold = threshold
        self._session_factory = session_factory
        self._db_lock = db_lock
        self._sample_limit = (
            sample_limit if sample_limit is not None else threshold
        )
        self._dict_size = dict_size
        self._done = False

    async def record_request(self) -> bool:
        """Count one completed request; train+recompress once at the threshold.

        Returns ``True`` on the single call that brings the count to
        ``threshold`` — having trained the dictionary and recompressed the
        step's responses on that call — and ``False`` every other time. Once
        it has fired it is inert: later calls neither count nor act.
        """
        if self._done:
            return False
        self.count += 1
        if self.count >= self.threshold:
            # Claim the one-shot job before the first await: record_request has
            # no await between the top guard and here, so setting _done now
            # makes check-and-claim atomic against the event loop. Concurrent
            # workers on the same step that cross the threshold during
            # _train_and_compact would otherwise each re-train and re-compress
            # (the shared db_lock serializes but does not dedupe them). With the
            # flag set first, only the first caller acts.
            self._done = True
            await self._train_and_compact()
            return True
        return False

    @property
    def done(self) -> bool:
        """Whether the train+recompress has already happened."""
        return self._done

    async def _train_and_compact(self) -> None:
        """Train a dictionary for the step and recompress its responses.

        Idempotent: if a dictionary already exists for the step the compaction
        is already done — skip the train+recompress rather than minting a
        redundant version. This covers a Compactor seeded at/above the
        threshold on a resumed run (its first ``record_request`` would
        otherwise re-train over a step that was already compacted).
        """
        existing = await get_compression_dict(
            self._session_factory, self.step, self._db_lock
        )
        if existing is not None:
            return
        dict_size = (
            self._dict_size
            if self._dict_size is not None
            else DEFAULT_DICT_SIZE
        )
        dict_id = await train_compression_dict(
            self._session_factory,
            self.step,
            sample_limit=self._sample_limit,
            dict_size=dict_size,
            db_lock=self._db_lock,
        )
        await recompress_responses(
            self._session_factory,
            self.step,
            dict_id=dict_id,
            db_lock=self._db_lock,
        )


class WorkerMonitor(Monitor):
    """Concrete :class:`Monitor`: scales the pool from an in-memory window.

    Each cycle reads pending work, rate-limit headroom, and recent request
    durations, then asks the run to spawn workers up to ``max_workers``. It
    scales up only; workers retire themselves on idle. Sizing reads the
    in-memory duration window (fed by workers) and the limiter's configured
    rate — never a database. Storage upkeep is the :class:`Compactor`'s job.
    """

    DEFAULT_POLL_INTERVAL = 60.0
    DEFAULT_WINDOW = 100

    def __init__(
        self,
        run: Run,
        rate_limiter: RateLimiter,
        *,
        max_workers: int,
        pending_requests: Callable[[], Awaitable[int]],
        stop_event: asyncio.Event | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        window: int = DEFAULT_WINDOW,
    ) -> None:
        self._run = run
        self._rate_limiter = rate_limiter
        self.max_workers = max_workers
        self._pending_requests = pending_requests
        self.stop_event = (
            stop_event if stop_event is not None else asyncio.Event()
        )
        self.poll_interval = poll_interval
        self._durations: deque[float] = deque(maxlen=window)

    async def run(self) -> None:
        """Loop until shutdown, sizing the pool each cycle."""
        idle_cycles = 0
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=self.poll_interval
                )
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # normal cycle

            active_count = self._run.active_worker_count
            pending_count = await self._pending_requests()
            if active_count == 0 and pending_count == 0:
                # Debounce: a single empty read can be a transient lull —
                # work is enqueued lazily as earlier responses resolve, and
                # the initial workers may not be spawned yet. Only treat the
                # run as finished after two consecutive idle cycles, so a
                # momentary gap doesn't permanently stop the monitor from
                # scaling once work reappears.
                idle_cycles += 1
                if idle_cycles >= 2:
                    break
                continue
            idle_cycles = 0

            if (
                pending_count > 0
                and active_count < self.workers_needed()
                and active_count < self.max_workers
            ):
                self._run.spawn_worker()

    def record_request_duration(self, duration_s: float) -> None:
        """Append one completed request's duration to the in-memory window."""
        self._durations.append(duration_s)

    def recent_avg_request_duration_s(self) -> float | None:
        """Mean of the window, or None until a duration is recorded.

        OPT_CAND: recomputes sum/len over the window (default 100) on each
        call. Negligible at the once-per-poll-cycle call site today; if a
        hot-path caller appears, switch to a running sum maintained in
        ``record_request_duration`` (subtract the evicted value on overflow)
        for O(1) reads.
        """
        if not self._durations:
            return None
        return sum(self._durations) / len(self._durations)

    def workers_needed(self) -> int:
        """Target pool size from rate headroom and recent avg duration."""
        avg = self.recent_avg_request_duration_s()
        max_rate = self._rate_limiter.max_rate_per_second
        if avg is None:
            target = self._run.active_worker_count + 1
        elif max_rate is None:
            target = self.max_workers
        else:
            target = math.ceil(max_rate * avg)
        return max(1, min(target, self.max_workers))
