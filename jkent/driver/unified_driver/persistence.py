"""Concrete persistence components for the unified driver.

``RequestQueue`` owns the unified driver's DB-backed queue: it subclasses
``database_engine.queue.RequestQueueDB`` (the shared (de)serialization /
dequeue / staged-enqueue methods) and adds the unified-specific glue — the
``_emit_progress`` hook and the progress-emitting ``enqueue_request``.
``ResponseStorage`` subclasses ``database_engine.storage.ResponseStorageDB``
(the shared lifecycle / response / result storage methods) and adds the
unified-specific ``max_backoff_time`` config; ``ReplayStorage`` adds the
replay-run terminal-state operations, delegating to the SQLManager (which
owns all the SQL).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jkent.data_types import Request, Response
from jkent.driver.database_engine.queue import RequestQueueDB
from jkent.driver.database_engine.storage import ResponseStorageDB

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from jkent.driver.database_engine.sql_manager import SQLManager


class RequestQueue(RequestQueueDB):
    """DB-backed request queue (enqueue/dequeue/(de)serialize) for the unified driver."""

    def __init__(
        self,
        db: SQLManager,
        *,
        on_progress: Callable[[str, dict[str, Any]], Awaitable[None]]
        | None = None,
    ) -> None:
        self.db = db
        self._on_progress = on_progress

    async def _emit_progress(
        self, event_type: str, data: dict[str, Any]
    ) -> None:
        """Forward a progress event to the injected callback, if any."""
        if self._on_progress is not None:
            await self._on_progress(event_type, data)

    async def enqueue_request(
        self,
        new_request: Request,
        context: Response | Request,
        parent_request_id: int | None = None,
    ) -> None:
        """Enqueue a new request to the database.

        Persists the request to SQLite.

        Args:
            new_request: The new request to enqueue.
            context: Response or originating request for URL resolution.
            parent_request_id: Optional parent request ID for tracking request relationships.
        """
        (
            request_data,
            dedup_key,
            parent_id,
            progress_event,
        ) = await self._prepare_enqueue(
            new_request, context, parent_request_id
        )

        # Skip duplicates (and suppress their progress event, matching the
        # staged path). The single committed-rows check here is authoritative,
        # so the insert skips its own redundant lookup; the uq_requests_dedup_key
        # ON CONFLICT IGNORE constraint still backstops a concurrent insert.
        if dedup_key and await self.db.check_dedup_key_exists(dedup_key):
            return

        await self.db.insert_request(
            dedup_key=dedup_key,
            parent_id=parent_id,
            skip_dedup_check=True,
            **request_data,
        )
        await self._emit_progress("request_enqueued", progress_event)


class ResponseStorage(ResponseStorageDB):
    """Response/result storage and retry/backoff handling for the unified driver."""

    def __init__(
        self,
        db: SQLManager,
        *,
        max_backoff_time: float = 3600.0,
        retry_base_delay: float = 1.0,
    ) -> None:
        self.db = db
        self.max_backoff_time = max_backoff_time
        self.retry_base_delay = retry_base_delay


class ReplayStorage(ResponseStorage):
    """Storage with the replay-run terminal states (stub / delete / finalize).

    A replayed run never carries ``failed`` rows out the back: a missed or
    errored request is either deleted (``--miss skip``) or marked ``stubbed``
    so a downstream ``jkent run`` re-fetches it. These thin wrappers are the
    terminal states the ``ReplayWorker`` and ``ReplayRun`` delegate here; the
    SQL itself lives on the SQLManager alongside the rest of the schema.
    """

    async def stub_request(self, request_id: int) -> None:
        """Mark a single row ``stubbed`` (see ``SQLManager.stub_request``)."""
        await self.db.stub_request(request_id)

    async def stub_with_reseedable_walk(self, request_id: int) -> None:
        """Stub the nearest reseedable anchor of ``request_id``.

        See ``SQLManager.stub_reseedable_anchor``.
        """
        await self.db.stub_reseedable_anchor(request_id)

    async def delete_request_row(self, request_id: int) -> None:
        """Delete a row + its subtree (see ``SQLManager.delete_request_subtree``)."""
        await self.db.delete_request_subtree(request_id)

    async def finalize_stubs(self) -> None:
        """End-of-run stub resolution (see ``SQLManager.finalize_stubs``)."""
        await self.db.finalize_stubs()
