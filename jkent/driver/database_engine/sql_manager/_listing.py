"""Listing and read-only query operations for SQLManager."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import case, func, select
from sqlmodel import col

from jkent.driver.database_engine.compression import (
    decompress_response,
)
from jkent.driver.database_engine.models import Request, Result
from jkent.driver.database_engine.sql_manager._types import (
    Page,
    RequestRecord,
    ResponseRecord,
    ResultRecord,
)
from jkent.driver.database_engine.stats import (
    get_stats,
)

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class ListingMixin:
    """Cross-model read-only listing and retrieval operations."""

    _lock: asyncio.Lock  # type: ignore[misc]
    _session_factory: async_sessionmaker  # type: ignore[misc]

    # --- Status ---

    async def get_run_status(
        self,
    ) -> Literal["unstarted", "in_progress", "done"]:
        """Check the current state of the scraper run.

        Returns:
            "unstarted": No requests in DB
            "in_progress": Pending or in_progress requests exist
            "done": No pending/in_progress but completed requests exist
        """
        # One pass over the requests table for both the total and the
        # active (pending/in_progress) count, instead of two count scans.
        async with self._session_factory() as session:
            result = await session.execute(
                select(
                    func.count(),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    col(Request.status).in_(
                                        ["pending", "in_progress"]
                                    ),
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                ).select_from(Request)
            )
            row = result.first()

        total_count = row[0] if row else 0
        active_count = row[1] if row else 0

        if active_count > 0:
            return "in_progress"
        if total_count == 0:
            return "unstarted"
        return "done"

    # --- Listing Operations ---

    async def _paginate(
        self,
        session: AsyncSession,
        *,
        count_from: Any,
        columns: tuple[Any, ...],
        conditions: list[Any],
        order_by: tuple[Any, ...],
        offset: int,
        limit: int,
        row_mapper: Callable[[Any], Any],
    ) -> Page[Any]:
        """Run a filtered count + page query and build a Page.

        Kept as two queries (count, then page) rather than a windowed
        ``count().over()`` so ``total`` stays correct when ``offset`` lands
        past the last matching row, which would otherwise return no rows to
        carry the window count.
        """
        count_stmt = select(func.count()).select_from(count_from)
        for cond in conditions:
            count_stmt = count_stmt.where(cond)
        total = (await session.execute(count_stmt)).scalar() or 0

        data_stmt = select(*columns)
        for cond in conditions:
            data_stmt = data_stmt.where(cond)
        data_stmt = data_stmt.order_by(*order_by).limit(limit).offset(offset)
        rows = (await session.execute(data_stmt)).all()

        return Page(
            items=[row_mapper(row) for row in rows],
            total=total,
            offset=offset,
            limit=limit,
        )

    async def list_requests(
        self,
        status: str | None = None,
        continuation: str | None = None,
        offset: int = 0,
        limit: int = 50,
        sort: str = "queue",
    ) -> Page[RequestRecord]:
        """List requests with optional filters and pagination.

        Args:
            status: Filter by status.
            continuation: Filter by continuation method name.
            offset: Number of records to skip.
            limit: Maximum number of records to return.
            sort: Sort order - "queue" (default: priority, queue_counter),
                  "id_asc" (by id ascending), or "id_desc" (by id descending).

        Returns:
            Page of RequestRecord instances.
        """
        conditions: list[Any] = []
        if status:
            conditions.append(col(Request.status) == status)
        if continuation:
            conditions.append(col(Request.continuation) == continuation)

        if sort == "id_asc":
            order_by: tuple[Any, ...] = (col(Request.id).asc(),)
        elif sort == "id_desc":
            order_by = (col(Request.id).desc(),)
        else:
            order_by = (
                col(Request.priority).asc(),
                col(Request.queue_counter).asc(),
            )

        async with self._session_factory() as session:
            return await self._paginate(
                session,
                count_from=Request,
                columns=RequestRecord.select_columns(Request),
                conditions=conditions,
                order_by=order_by,
                offset=offset,
                limit=limit,
                row_mapper=RequestRecord.from_row,
            )

    async def list_responses(
        self,
        continuation: str | None = None,
        request_id: int | None = None,
        speculation_outcome: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> Page[ResponseRecord]:
        """List responses with optional filters and pagination.

        Queries requests that have a response (response_status_code IS NOT NULL).

        Args:
            continuation: Filter by continuation method name.
            request_id: Filter by request ID.
            speculation_outcome: Filter by speculation outcome.
            offset: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            Page of ResponseRecord instances.
        """
        conditions: list[Any] = [col(Request.response_status_code).isnot(None)]
        if continuation:
            conditions.append(col(Request.continuation) == continuation)
        if request_id:
            conditions.append(col(Request.id) == request_id)
        if speculation_outcome:
            conditions.append(
                col(Request.speculation_outcome) == speculation_outcome
            )

        async with self._session_factory() as session:
            return await self._paginate(
                session,
                count_from=Request,
                columns=ResponseRecord.select_columns(Request),
                conditions=conditions,
                order_by=(col(Request.id).desc(),),
                offset=offset,
                limit=limit,
                row_mapper=ResponseRecord.from_row,
            )

    async def list_results(
        self,
        result_type: str | None = None,
        is_valid: bool | None = None,
        request_id: int | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> Page[ResultRecord]:
        """List results with optional filters and pagination.

        Args:
            result_type: Filter by result type.
            is_valid: Filter by validation status.
            request_id: Filter by request ID.
            offset: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            Page of ResultRecord instances.
        """
        conditions: list[Any] = []
        if result_type:
            conditions.append(col(Result.result_type) == result_type)
        if is_valid is not None:
            conditions.append(col(Result.is_valid) == is_valid)
        if request_id:
            conditions.append(col(Result.request_id) == request_id)

        async with self._session_factory() as session:
            return await self._paginate(
                session,
                count_from=Result,
                columns=ResultRecord.select_columns(Result),
                conditions=conditions,
                order_by=(col(Result.id).desc(),),
                offset=offset,
                limit=limit,
                row_mapper=ResultRecord.from_row,
            )

    async def get_request(self, request_id: int) -> RequestRecord | None:
        """Get a single request by ID.

        Args:
            request_id: The database ID of the request.

        Returns:
            RequestRecord or None if not found.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(*RequestRecord.select_columns(Request)).where(
                    col(Request.id) == request_id
                )
            )
            row = result.first()
            if row is None:
                return None
            return RequestRecord.from_row(row)

    async def get_response(self, request_id: int) -> ResponseRecord | None:
        """Get response data for a request by its ID.

        Args:
            request_id: The database ID of the request.

        Returns:
            ResponseRecord or None if not found or no response stored.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(*ResponseRecord.select_columns(Request)).where(
                    col(Request.id) == request_id,
                    col(Request.response_status_code).isnot(None),
                )
            )
            row = result.first()
            return ResponseRecord.from_row(row) if row is not None else None

    async def get_result(self, result_id: int) -> ResultRecord | None:
        """Get a single result by ID.

        Args:
            result_id: The database ID of the result.

        Returns:
            ResultRecord or None if not found.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(*ResultRecord.select_columns(Result)).where(
                    col(Result.id) == result_id
                )
            )
            row = result.first()
            return ResultRecord.from_row(row) if row is not None else None

    # --- Resume Request Operations ---

    async def get_permanent_json(self, request_id: int) -> str | None:
        """Get permanent_json field for a request.

        Args:
            request_id: The database ID of the request.

        Returns:
            The permanent_json string or None.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(col(Request.permanent_json)).where(
                    col(Request.id) == request_id
                )
            )
            return result.scalar()

    # --- Statistics ---

    async def get_stats(self) -> Any:
        """Get comprehensive statistics about the driver state.

        Returns:
            DevDriverStats instance.
        """
        return await get_stats(self._session_factory)

    # --- Response Content Access ---

    async def get_response_content(self, request_id: int) -> bytes | None:
        """Get decompressed response content by request ID.

        Args:
            request_id: The database ID of the request.

        Returns:
            Decompressed content bytes, or None if request not found
            or has no response.
        """
        result = await self.get_response_compressed(request_id)  # type: ignore[attr-defined]
        if result is None:
            return None

        compressed, dict_id = result
        if not compressed:
            return b""

        return await decompress_response(
            self._session_factory, compressed, dict_id, db_lock=self._lock
        )
