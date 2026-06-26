"""Request queue operations for SQLManager."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, delete, func, or_, select, update
from sqlmodel import col

from jkent.driver.database_engine.models import (
    ArchivedFile,
    Error,
    Estimate,
    Request,
    Result,
)
from jkent.driver.database_engine.sql_manager._types import compute_cache_key

if TYPE_CHECKING:
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class RequestQueueMixin:
    """Request table database operations."""

    _lock: asyncio.Lock  # type: ignore[misc]
    _session_factory: async_sessionmaker  # type: ignore[misc]

    async def check_dedup_key_exists(self, dedup_key: str) -> bool:
        """Check if a deduplication key already exists.

        Args:
            dedup_key: The deduplication key to check.

        Returns:
            True if the key exists, False otherwise.
        """
        async with self._session_factory() as session:
            return (
                await self._find_by_dedup_key_in_session(session, dedup_key)
                is not None
            )

    async def _find_by_dedup_key_in_session(
        self, session: AsyncSession, dedup_key: str
    ) -> int | None:
        """Find a request ID by deduplication key inside an existing session."""
        result = await session.execute(
            select(col(Request.id)).where(
                col(Request.deduplication_key) == dedup_key
            )
        )
        return result.scalar()

    async def _find_existing_dedup_keys_in_session(
        self, session: AsyncSession, dedup_keys: list[str]
    ) -> set[str]:
        """Return which of *dedup_keys* already exist as requests.

        One query per chunk instead of a point lookup per key, so a step that
        yields many deduplicated children does not issue an N+1 of selects.
        Chunked to stay under SQLite's bound-parameter limit.
        """
        existing: set[str] = set()
        for start in range(0, len(dedup_keys), 500):
            chunk = dedup_keys[start : start + 500]
            result = await session.execute(
                select(col(Request.deduplication_key)).where(
                    col(Request.deduplication_key).in_(chunk)
                )
            )
            existing.update(row[0] for row in result.all())
        return existing

    async def _get_next_queue_counter_in_session(
        self, session: AsyncSession
    ) -> int:
        """Next queue counter, computed in memory after a one-time seed.

        Reuses the caller's session for the one-time
        ``max(queue_counter)`` seed so no extra connection is opened.
        """
        await self._ensure_queue_counter_seeded(session)  # type: ignore[attr-defined]
        assert self._queue_counter is not None  # type: ignore[attr-defined, has-type]
        self._queue_counter += 1  # type: ignore[attr-defined]
        return self._queue_counter  # type: ignore[attr-defined]

    async def find_parent_request_id(self, url: str) -> int | None:
        """Find the request ID for a given URL.

        Used to link child requests to their parent.

        Args:
            url: The URL of the parent request.

        Returns:
            Request ID if found, None otherwise.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(col(Request.id))
                .where(
                    col(Request.url) == url,
                    col(Request.status).in_(["completed", "in_progress"]),
                )
                .order_by(col(Request.id).desc())
                .limit(1)
            )
            return result.scalar()

    async def insert_request(
        self,
        priority: int,
        request_type: str,
        method: str,
        url: str,
        headers_json: str | None,
        cookies_json: str | None,
        body: bytes | None,
        continuation: str,
        current_location: str,
        accumulated_data_json: str | None,
        permanent_json: str | None,
        expected_type: str | None,
        dedup_key: str | None,
        parent_id: int | None,
        is_speculative: bool = False,
        speculation_id: str | None = None,
        verify: str | None = None,
        via_json: str | None = None,
        bypass_rate_limit: bool = False,
        timeout_json: str | None = None,
        json_data: str | None = None,
        files_json: str | None = None,
        auth_json: str | None = None,
        allow_redirects: bool = True,
        proxies_json: str | None = None,
        stream: bool = False,
        cert_json: str | None = None,
        archive_hash_header: str | None = None,
        reseedable: bool | None = None,
        skip_dedup_check: bool = False,
    ) -> int:
        """Insert a new request into the queue.

        Args:
            priority: Request priority (lower = higher priority).
            request_type: Type of request (navigating, non_navigating, etc.).
            method: HTTP method.
            url: Request URL.
            headers_json: JSON-encoded headers.
            cookies_json: JSON-encoded cookies.
            body: Request body bytes.
            continuation: Continuation method name.
            current_location: Current navigation location.
            accumulated_data_json: JSON-encoded accumulated data.
            permanent_json: JSON-encoded permanent data.
            expected_type: Expected type for archive requests.
            dedup_key: Deduplication key.
            parent_id: Parent request ID.
            is_speculative: Whether this is a speculative request.
            speculation_id: JSON tuple ["func_name", spec_id] for speculative requests.
            bypass_rate_limit: If True, skip rate limiting for this request.
            skip_dedup_check: Pass ``True`` when the caller has already checked
                the dedup key against committed rows, to avoid re-running the
                same lookup inside this insert (the ``uq_requests_dedup_key``
                ON CONFLICT IGNORE constraint still backstops races).

        Returns:
            The ID of the newly inserted request, or the existing ID if
            deduplicated.
        """
        async with self._lock, self._session_factory() as session:
            req_id = await self.insert_request_in_session(
                session,
                priority=priority,
                request_type=request_type,
                method=method,
                url=url,
                headers_json=headers_json,
                cookies_json=cookies_json,
                body=body,
                continuation=continuation,
                current_location=current_location,
                accumulated_data_json=accumulated_data_json,
                permanent_json=permanent_json,
                expected_type=expected_type,
                dedup_key=dedup_key,
                parent_id=parent_id,
                is_speculative=is_speculative,
                speculation_id=speculation_id,
                verify=verify,
                via_json=via_json,
                bypass_rate_limit=bypass_rate_limit,
                timeout_json=timeout_json,
                json_data=json_data,
                files_json=files_json,
                auth_json=auth_json,
                allow_redirects=allow_redirects,
                proxies_json=proxies_json,
                stream=stream,
                cert_json=cert_json,
                archive_hash_header=archive_hash_header,
                reseedable=reseedable,
                skip_dedup_check=skip_dedup_check,
            )
            await session.commit()
            return req_id

    async def insert_request_in_session(
        self,
        session: AsyncSession,
        *,
        priority: int,
        request_type: str,
        method: str,
        url: str,
        headers_json: str | None,
        cookies_json: str | None,
        body: bytes | None,
        continuation: str,
        current_location: str,
        accumulated_data_json: str | None,
        permanent_json: str | None,
        expected_type: str | None,
        dedup_key: str | None,
        parent_id: int | None,
        is_speculative: bool = False,
        speculation_id: str | None = None,
        verify: str | None = None,
        via_json: str | None = None,
        bypass_rate_limit: bool = False,
        timeout_json: str | None = None,
        json_data: str | None = None,
        files_json: str | None = None,
        auth_json: str | None = None,
        allow_redirects: bool = True,
        proxies_json: str | None = None,
        stream: bool = False,
        cert_json: str | None = None,
        archive_hash_header: str | None = None,
        reseedable: bool | None = None,
        skip_dedup_check: bool = False,
    ) -> int:
        """Insert a request inside an existing session (no commit).

        Performs the dedup check and INSERT in the same session so callers
        can compose multiple writes into a single transaction. Pass
        ``skip_dedup_check=True`` when the caller has already checked the
        dedup key against committed rows (e.g. a batched staging flush) to
        avoid a redundant per-row lookup.
        """
        if not skip_dedup_check and dedup_key is not None:
            existing = await self._find_by_dedup_key_in_session(
                session, dedup_key
            )
            if existing is not None:
                return existing

        queue_counter = await self._get_next_queue_counter_in_session(session)
        created_at_ns = time.monotonic_ns()
        cache_key = compute_cache_key(method, url, body, headers_json)

        req = Request(
            status="pending",
            priority=priority,
            queue_counter=queue_counter,
            request_type=request_type,
            method=method,
            url=url,
            headers_json=headers_json,
            cookies_json=cookies_json,
            body=body,
            continuation=continuation,
            current_location=current_location,
            accumulated_data_json=accumulated_data_json,
            permanent_json=permanent_json,
            expected_type=expected_type,
            deduplication_key=dedup_key,
            parent_request_id=parent_id,
            created_at_ns=created_at_ns,
            cache_key=cache_key,
            is_speculative=is_speculative,
            speculation_id=speculation_id,
            verify=verify,
            via_json=via_json,
            bypass_rate_limit=bypass_rate_limit,
            timeout_json=timeout_json,
            json_data=json_data,
            files_json=files_json,
            auth_json=auth_json,
            allow_redirects=allow_redirects,
            proxies_json=proxies_json,
            stream=stream,
            cert_json=cert_json,
            archive_hash_header=archive_hash_header,
            reseedable=reseedable,
        )
        session.add(req)
        await session.flush()
        return req.id  # type: ignore[return-value]

    async def get_next_pending_request(
        self,
    ) -> tuple[Any, ...] | None:
        """Get the next pending request from the queue.

        Returns:
            Row tuple or None if queue is empty.

        Note: This method is deprecated for multi-worker scenarios.
        Use dequeue_next_request() instead for atomic dequeue.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(
                    col(Request.id),
                    col(Request.request_type),
                    col(Request.method),
                    col(Request.url),
                    col(Request.headers_json),
                    col(Request.cookies_json),
                    col(Request.body),
                    col(Request.continuation),
                    col(Request.current_location),
                    col(Request.accumulated_data_json),
                    col(Request.permanent_json),
                    col(Request.expected_type),
                    col(Request.priority),
                    col(Request.is_speculative),
                    col(Request.speculation_id),
                    col(Request.verify),
                    col(Request.via_json),
                    col(Request.bypass_rate_limit),
                    col(Request.deduplication_key),
                    col(Request.timeout_json),
                    col(Request.json_data),
                    col(Request.files_json),
                    col(Request.auth_json),
                    col(Request.allow_redirects),
                    col(Request.proxies_json),
                    col(Request.stream),
                    col(Request.cert_json),
                    col(Request.archive_hash_header),
                    col(Request.reseedable),
                )
                .where(
                    col(Request.status) == "pending",
                    or_(
                        col(Request.started_at).is_(None),
                        col(Request.started_at) <= func.datetime("now"),
                    ),
                )
                .order_by(
                    col(Request.priority).asc(),
                    col(Request.queue_counter).asc(),
                )
                .limit(1)
            )
            row = result.first()
            return tuple(row) if row else None

    async def dequeue_next_request(
        self,
    ) -> tuple[Any, ...] | None:
        """Atomically dequeue the next pending request.

        This method atomically selects and marks a request as 'in_progress'
        in a single database operation using UPDATE ... RETURNING. This
        prevents race conditions where multiple workers could select the
        same request.

        Returns:
            Row tuple (same columns as get_next_pending_request) or None
            if the queue is empty.
        """
        async with self._lock, self._session_factory() as session:
            started_at_ns = time.monotonic_ns()

            subq = (
                select(col(Request.id))
                .where(
                    col(Request.status) == "pending",
                    or_(
                        col(Request.started_at).is_(None),
                        col(Request.started_at) <= func.datetime("now"),
                    ),
                )
                .order_by(
                    col(Request.priority).asc(),
                    col(Request.queue_counter).asc(),
                )
                .limit(1)
                .scalar_subquery()
            )

            stmt = (
                update(Request)
                .where(col(Request.id) == subq)
                .values(
                    status="in_progress",
                    started_at=func.current_timestamp(),
                    started_at_ns=started_at_ns,
                )
                .returning(
                    col(Request.id),
                    col(Request.request_type),
                    col(Request.method),
                    col(Request.url),
                    col(Request.headers_json),
                    col(Request.cookies_json),
                    col(Request.body),
                    col(Request.continuation),
                    col(Request.current_location),
                    col(Request.accumulated_data_json),
                    col(Request.permanent_json),
                    col(Request.expected_type),
                    col(Request.priority),
                    col(Request.is_speculative),
                    col(Request.speculation_id),
                    col(Request.verify),
                    col(Request.via_json),
                    col(Request.bypass_rate_limit),
                    col(Request.deduplication_key),
                    col(Request.timeout_json),
                    col(Request.json_data),
                    col(Request.files_json),
                    col(Request.auth_json),
                    col(Request.allow_redirects),
                    col(Request.proxies_json),
                    col(Request.stream),
                    col(Request.cert_json),
                    col(Request.archive_hash_header),
                    col(Request.reseedable),
                    col(Request.parent_request_id),
                )
            )
            result = await session.execute(stmt)
            row = result.first()
            await session.commit()
            return tuple(row) if row else None

    async def mark_request_in_progress(self, request_id: int) -> None:
        """Mark a request as in progress.

        Args:
            request_id: The database ID of the request.

        Note: This method is deprecated for multi-worker scenarios.
        Use dequeue_next_request() instead for atomic dequeue.
        """
        async with self._lock, self._session_factory() as session:
            started_at_ns = time.monotonic_ns()
            await session.execute(
                update(Request)
                .where(col(Request.id) == request_id)
                .values(
                    status="in_progress",
                    started_at=func.current_timestamp(),
                    started_at_ns=started_at_ns,
                )
            )
            await session.commit()

    async def mark_request_completed(self, request_id: int) -> None:
        """Mark a request as completed.

        Args:
            request_id: The database ID of the request.
        """
        async with self._lock, self._session_factory() as session:
            await self.mark_request_completed_in_session(session, request_id)
            await session.commit()

    async def mark_request_completed_in_session(
        self, session: AsyncSession, request_id: int
    ) -> None:
        """Mark a request as completed inside an existing session (no commit)."""
        completed_at_ns = time.monotonic_ns()
        await session.execute(
            update(Request)
            .where(col(Request.id) == request_id)
            .values(
                status="completed",
                completed_at=func.current_timestamp(),
                completed_at_ns=completed_at_ns,
            )
        )

    async def mark_request_failed(
        self, request_id: int, error_message: str
    ) -> None:
        """Mark a request as failed.

        Args:
            request_id: The database ID of the request.
            error_message: Error message describing the failure.
        """
        async with self._lock, self._session_factory() as session:
            completed_at_ns = time.monotonic_ns()
            await session.execute(
                update(Request)
                .where(col(Request.id) == request_id)
                .values(
                    status="failed",
                    completed_at=func.current_timestamp(),
                    completed_at_ns=completed_at_ns,
                    last_error=error_message,
                )
            )
            await session.commit()

    async def get_retry_state(
        self, request_id: int
    ) -> tuple[int, float] | None:
        """Get retry state for a request.

        Args:
            request_id: The database ID of the request.

        Returns:
            Tuple of (retry_count, cumulative_backoff) or None if not found.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(
                    col(Request.retry_count), col(Request.cumulative_backoff)
                ).where(col(Request.id) == request_id)
            )
            row = result.first()
            if row is None:
                return None
            return (row[0], row[1] or 0.0)

    async def schedule_retry(
        self,
        request_id: int,
        new_cumulative_backoff: float,
        next_retry_delay: float,
        error: str,
    ) -> None:
        """Schedule a request for retry with backoff.

        Args:
            request_id: The database ID of the request.
            new_cumulative_backoff: Updated cumulative backoff time.
            next_retry_delay: Delay before next retry.
            error: Error message from the current attempt.
        """
        async with self._lock, self._session_factory() as session:
            await session.execute(
                update(Request)
                .where(col(Request.id) == request_id)
                .values(
                    status="pending",
                    retry_count=col(Request.retry_count) + 1,
                    cumulative_backoff=new_cumulative_backoff,
                    next_retry_delay=next_retry_delay,
                    last_error=error,
                    started_at=func.datetime(
                        "now",
                        f"+{int(next_retry_delay)} seconds",
                    ),
                )
            )
            await session.commit()

    async def count_pending_requests(self) -> int:
        """Count pending requests in the queue."""
        async with self._lock, self._session_factory() as session:
            result = await session.execute(
                select(func.count())
                .select_from(Request)
                .where(col(Request.status) == "pending")
            )
            return result.scalar() or 0

    async def seconds_until_next_pending(self) -> float | None:
        """Seconds until the soonest pending request becomes dequeuable.

        Returns 0.0 when a pending request is ready now (``started_at`` is
        NULL or already past — e.g. a retry whose backoff has elapsed), the
        positive gap until the soonest future-scheduled request when every
        pending request is still in retry backoff, or None when there are no
        pending requests at all. Lets a worker that just dequeued None sleep
        exactly until the next retry is ready instead of retiring and leaving
        it to the slow monitor poll.
        """
        async with self._lock, self._session_factory() as session:
            result = await session.execute(
                select(
                    func.min(
                        case(
                            (
                                or_(
                                    col(Request.started_at).is_(None),
                                    col(Request.started_at)
                                    <= func.datetime("now"),
                                ),
                                0.0,
                            ),
                            else_=(
                                func.julianday(col(Request.started_at))
                                - func.julianday(func.datetime("now"))
                            )
                            * 86400.0,
                        )
                    )
                ).where(col(Request.status) == "pending")
            )
            value = result.scalar()
            if value is None:
                return None
            return max(0.0, float(value))

    async def restamp_request_start(self, request_id: int) -> None:
        """Reset a request's start timestamps to now, after the rate gate.

        ``started_at`` is stamped at dequeue, before the rate-limiter gate, so
        a DB-derived duration would otherwise include time spent waiting for a
        token. Re-stamping just before execution makes the persisted start
        mark the execute region rather than the queue wait.
        """
        async with self._lock, self._session_factory() as session:
            await session.execute(
                update(Request)
                .where(col(Request.id) == request_id)
                .values(
                    started_at=func.current_timestamp(),
                    started_at_ns=time.monotonic_ns(),
                )
            )
            await session.commit()

    async def count_active_requests(self) -> int:
        """Count pending and in_progress requests."""
        async with self._lock, self._session_factory() as session:
            result = await session.execute(
                select(func.count())
                .select_from(Request)
                .where(col(Request.status).in_(["pending", "in_progress"]))
            )
            return result.scalar() or 0

    async def count_all_requests(self) -> int:
        """Count all requests in the database."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count()).select_from(Request)
            )
            return result.scalar() or 0

    async def avg_completed_request_duration_s(
        self, sample_size: int = 20
    ) -> float | None:
        """Average duration of recently completed requests, in seconds.

        Uses the high-precision monotonic timestamps (started_at_ns,
        completed_at_ns) from the last *sample_size* completed requests.

        Returns:
            Average duration in seconds, or None if no completed
            requests with timing data exist.
        """
        subq = (
            select(
                (
                    col(Request.completed_at_ns) - col(Request.started_at_ns)
                ).label("duration_ns")
            )
            .where(
                col(Request.status) == "completed",
                col(Request.started_at_ns).isnot(None),
                col(Request.completed_at_ns).isnot(None),
            )
            .order_by(col(Request.id).desc())
            .limit(sample_size)
            .subquery()
        )
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.avg(subq.c.duration_ns))
            )
            avg_ns = result.scalar()
            if avg_ns is None or avg_ns <= 0:
                return None
            return avg_ns / 1_000_000_000

    async def continuations_needing_compression_dict(
        self, threshold: int = 1000
    ) -> list[str]:
        """Find continuations with enough responses to train a dictionary.

        Returns continuations that have at least *threshold* responses
        whose ``compression_dict_id`` is NULL (i.e. not yet compressed
        with a trained dictionary).

        Args:
            threshold: Minimum number of undict-compressed responses
                required before a continuation is returned.

        Returns:
            List of continuation names meeting the threshold.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(col(Request.continuation))
                .where(
                    col(Request.response_status_code).isnot(None),
                    col(Request.content_compressed).isnot(None),
                    col(Request.compression_dict_id).is_(None),
                )
                .group_by(col(Request.continuation))
                .having(func.count() >= threshold)
            )
            return [row[0] for row in result.all()]

    async def resolved_response_count(self, continuation: str) -> int:
        """Count a continuation's requests that carry a compressible body.

        Mirrors ``train_compression_dict``'s own filter (a stored
        ``response_status_code`` *and* a non-NULL ``content_compressed``):
        archive requests set a status code but store no body, so counting
        them would seed a compactor that then trains over zero responses.
        Counting only rows with a body keeps the seed and the training set
        consistent.
        """
        async with self._lock, self._session_factory() as session:
            result = await session.execute(
                select(func.count())
                .select_from(Request)
                .where(
                    col(Request.continuation) == continuation,
                    col(Request.response_status_code).isnot(None),
                    col(Request.content_compressed).isnot(None),
                )
            )
            return result.scalar() or 0

    # --- Step Control ---

    async def pause_step(self, continuation: str) -> int:
        """Pause processing of requests for a continuation.

        Marks all pending requests as 'held'.

        Args:
            continuation: The continuation method name.

        Returns:
            Number of requests marked as held.
        """
        async with self._lock, self._session_factory() as session:
            result = await session.execute(
                update(Request)
                .where(
                    col(Request.status) == "pending",
                    col(Request.continuation) == continuation,
                )
                .values(status="held")
            )
            await session.commit()
            return result.rowcount  # type: ignore[attr-defined]

    async def resume_step(self, continuation: str) -> int:
        """Resume processing of held requests.

        Args:
            continuation: The continuation method name.

        Returns:
            Number of requests restored to pending.
        """
        async with self._lock, self._session_factory() as session:
            result = await session.execute(
                update(Request)
                .where(
                    col(Request.status) == "held",
                    col(Request.continuation) == continuation,
                )
                .values(status="pending")
            )
            await session.commit()
            return result.rowcount  # type: ignore[attr-defined]

    async def get_held_count(self, continuation: str | None = None) -> int:
        """Get count of held requests.

        Args:
            continuation: Optional continuation name filter.

        Returns:
            Count of held requests.
        """
        async with self._session_factory() as session:
            stmt = (
                select(func.count())
                .select_from(Request)
                .where(col(Request.status) == "held")
            )
            if continuation:
                stmt = stmt.where(col(Request.continuation) == continuation)
            result = await session.execute(stmt)
            return result.scalar() or 0

    # --- Request Cancellation ---

    async def cancel_request(self, request_id: int) -> bool:
        """Cancel a pending request.

        Args:
            request_id: The database ID of the request.

        Returns:
            True if cancelled, False if not found or not cancellable.
        """
        async with self._lock, self._session_factory() as session:
            completed_at_ns = time.monotonic_ns()
            result = await session.execute(
                update(Request)
                .where(
                    col(Request.id) == request_id,
                    col(Request.status).in_(["pending", "held"]),
                )
                .values(
                    status="failed",
                    completed_at=func.current_timestamp(),
                    completed_at_ns=completed_at_ns,
                    last_error="Cancelled by user",
                )
            )
            await session.commit()
            return result.rowcount > 0  # type: ignore[attr-defined]

    async def cancel_requests_by_continuation(self, continuation: str) -> int:
        """Cancel all pending/held requests for a continuation.

        Args:
            continuation: The continuation method name.

        Returns:
            Number of requests cancelled.
        """
        async with self._lock, self._session_factory() as session:
            completed_at_ns = time.monotonic_ns()
            result = await session.execute(
                update(Request)
                .where(
                    col(Request.continuation) == continuation,
                    col(Request.status).in_(["pending", "held"]),
                )
                .values(
                    status="failed",
                    completed_at=func.current_timestamp(),
                    completed_at_ns=completed_at_ns,
                    last_error="Cancelled by user (batch)",
                )
            )
            await session.commit()
            return result.rowcount  # type: ignore[attr-defined]

    # --- Replay terminal states (stub / skip / finalize) ---------------------
    # These back the replay driver's miss handling. ``stubbed`` is an
    # intermediate status only ``finalize_stubs`` clears; a replay run that
    # never reaches finalize is discarded and rebuilt from scratch, so stranded
    # ``stubbed`` rows are never observed by a downstream run.

    async def _stub_row_in_session(
        self, session: AsyncSession, request_id: int
    ) -> None:
        """Mark one row ``stubbed`` and drop the outputs it produced (no commit).

        Clears the start timestamps and deletes everything the row's processing
        produced — results, archived-file metadata, estimates, and errors — so
        that re-fetching the re-pended row in a downstream live ``jkent run``
        cannot append a second copy alongside the originals. (A still-failing
        request just re-produces its error on the live run.) The stored
        *response* (content / status / headers) is deliberately kept: a
        downstream replay re-serves it, which keeps replay-of-replay idempotent.
        """
        await session.execute(
            update(Request)
            .where(col(Request.id) == request_id)
            .values(status="stubbed", started_at=None, started_at_ns=None)
        )
        for model in (Result, ArchivedFile, Estimate, Error):
            await session.execute(
                delete(model).where(col(model.request_id) == request_id)
            )

    async def stub_request(self, request_id: int) -> None:
        """Mark a single request ``stubbed`` and drop the outputs it produced.

        ``finalize_stubs`` later resets the row to ``pending`` so a downstream
        ``jkent run`` re-fetches it. See :meth:`_stub_row_in_session`.
        """
        async with self._lock, self._session_factory() as session:
            await self._stub_row_in_session(session, request_id)
            await session.commit()

    async def stub_reseedable_anchor(self, request_id: int) -> None:
        """Walk ``parent_request_id`` to the nearest reseedable anchor and stub it.

        Walks the output DB's runtime ancestry from the row that just failed,
        stopping at the first ``reseedable=True`` row or the root. Only the
        anchor is stubbed (and its produced outputs dropped, see
        :meth:`_stub_row_in_session`); :meth:`finalize_stubs` later drops its
        descendants, preserving the invariant that a re-pended row has no
        descendants.
        """
        async with self._lock, self._session_factory() as session:
            anchor_id = request_id
            current_id: int | None = request_id
            seen: set[int] = set()
            while current_id is not None and current_id not in seen:
                seen.add(current_id)
                row = (
                    await session.execute(
                        select(
                            col(Request.id),
                            col(Request.parent_request_id),
                            col(Request.reseedable),
                        ).where(col(Request.id) == current_id)
                    )
                ).first()
                if row is None:
                    break
                anchor_id = row[0]
                if row[2]:
                    break
                current_id = row[1]

            await self._stub_row_in_session(session, anchor_id)
            await session.commit()

    async def delete_request_subtree(self, request_id: int) -> None:
        """Delete a request row and its whole subtree (``--miss skip``).

        ``ON DELETE CASCADE`` on ``requests.parent_request_id`` (and on every
        child table's FK) drops the row's descendants and child-table rows in
        one statement, so the worker need not know the FK graph.
        """
        async with self._lock, self._session_factory() as session:
            await session.execute(
                delete(Request).where(col(Request.id) == request_id)
            )
            await session.commit()

    async def finalize_stubs(self) -> None:
        """End-of-run: drop stub descendants, then flip each stub to ``pending``.

        1. Delete the direct children of every ``stubbed`` row. The
           self-referential ``ON DELETE CASCADE`` propagates down the whole
           subtree, and each row's child-table FKs cascade too — so this one
           statement clears every descendant and its outputs, preserving the
           invariant that a re-pended row has no descendants.
        2. Flip the surviving (anchor-only) ``stubbed`` rows to ``pending``.

        Each anchor keeps its stored *response* (so a downstream replay can
        re-serve it) but, by the time it was stubbed, already had its *produced*
        outputs dropped (see :meth:`_stub_row_in_session`) — so a downstream
        live ``jkent run`` that re-fetches the ``pending`` row regenerates those
        outputs instead of duplicating them. (Start timestamps were cleared at
        stub time.)
        """
        stubbed_ids = (
            select(col(Request.id))
            .where(col(Request.status) == "stubbed")
            .scalar_subquery()
        )
        async with self._lock, self._session_factory() as session:
            await session.execute(
                delete(Request).where(
                    col(Request.parent_request_id).in_(stubbed_ids)
                )
            )
            await session.execute(
                update(Request)
                .where(col(Request.status) == "stubbed")
                .values(status="pending")
            )
            await session.commit()
