"""Listing and read-only query operations for SQLManager."""

from __future__ import annotations

import json
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

    from jkent.driver.database_engine.scoped_session import (
        ScopedSessionFactory,
    )


class ListingMixin:
    """Cross-model read-only listing and retrieval operations."""

    _lock: asyncio.Lock  # type: ignore[misc]
    _session_factory: ScopedSessionFactory  # type: ignore[misc]

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
        async with self._session_factory() as session:
            # Build WHERE conditions
            conditions = []
            if status:
                conditions.append(col(Request.status) == status)
            if continuation:
                conditions.append(col(Request.continuation) == continuation)

            # Count query
            count_stmt = select(func.count()).select_from(Request)
            for cond in conditions:
                count_stmt = count_stmt.where(cond)
            result = await session.execute(count_stmt)
            total = result.scalar() or 0

            # Data query
            data_stmt = select(*RequestRecord.select_columns(Request))
            for cond in conditions:
                data_stmt = data_stmt.where(cond)

            if sort == "id_asc":
                data_stmt = data_stmt.order_by(col(Request.id).asc())
            elif sort == "id_desc":
                data_stmt = data_stmt.order_by(col(Request.id).desc())
            else:
                data_stmt = data_stmt.order_by(
                    col(Request.priority).asc(),
                    col(Request.queue_counter).asc(),
                )

            data_stmt = data_stmt.limit(limit).offset(offset)
            result = await session.execute(data_stmt)
            rows = result.all()

            items = [RequestRecord.from_row(row) for row in rows]

            return Page(
                items=items,
                total=total,
                offset=offset,
                limit=limit,
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
        async with self._session_factory() as session:
            conditions = [col(Request.response_status_code).isnot(None)]
            if continuation:
                conditions.append(col(Request.continuation) == continuation)  # type: ignore[arg-type]
            if request_id:
                conditions.append(col(Request.id) == request_id)  # type: ignore[arg-type]
            if speculation_outcome:
                conditions.append(
                    col(Request.speculation_outcome) == speculation_outcome  # type: ignore[arg-type]
                )

            count_stmt = select(func.count()).select_from(Request)
            for cond in conditions:
                count_stmt = count_stmt.where(cond)
            result = await session.execute(count_stmt)
            total = result.scalar() or 0

            data_stmt = select(
                col(Request.id),
                col(Request.response_status_code),
                col(Request.response_url),
                col(Request.content_size_original),
                col(Request.content_size_compressed),
                col(Request.continuation),
                col(Request.response_created_at),
                col(Request.compression_dict_id),
                col(Request.speculation_outcome),
            )
            for cond in conditions:
                data_stmt = data_stmt.where(cond)
            data_stmt = (
                data_stmt.order_by(col(Request.id).desc())
                .limit(limit)
                .offset(offset)
            )

            result = await session.execute(data_stmt)  # type: ignore[assignment]
            rows = result.all()

            items = [
                ResponseRecord(
                    id=row[0],
                    status_code=row[1],
                    url=row[2],
                    content_size_original=row[3],
                    content_size_compressed=row[4],
                    continuation=row[5],
                    created_at=row[6],
                    compression_dict_id=row[7],
                    speculation_outcome=row[8],
                )
                for row in rows
            ]

            return Page(
                items=items,
                total=total,
                offset=offset,
                limit=limit,
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
        async with self._session_factory() as session:
            conditions = []
            if result_type:
                conditions.append(col(Result.result_type) == result_type)
            if is_valid is not None:
                conditions.append(col(Result.is_valid) == is_valid)
            if request_id:
                conditions.append(col(Result.request_id) == request_id)

            count_stmt = select(func.count()).select_from(Result)
            for cond in conditions:
                count_stmt = count_stmt.where(cond)
            result = await session.execute(count_stmt)
            total = result.scalar() or 0

            data_stmt = select(
                col(Result.id),
                col(Result.request_id),
                col(Result.result_type),
                col(Result.data_json),
                col(Result.is_valid),
                col(Result.validation_errors_json),
                col(Result.created_at),
            )
            for cond in conditions:
                data_stmt = data_stmt.where(cond)
            data_stmt = (
                data_stmt.order_by(col(Result.id).desc())
                .limit(limit)
                .offset(offset)
            )

            result = await session.execute(data_stmt)  # type: ignore[assignment]
            rows = result.all()

            items = [
                ResultRecord(
                    id=row[0],
                    request_id=row[1],
                    result_type=row[2],
                    data_json=row[3],
                    is_valid=bool(row[4]),
                    validation_errors_json=row[5],
                    created_at=row[6],
                )
                for row in rows
            ]

            return Page(
                items=items,
                total=total,
                offset=offset,
                limit=limit,
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
                select(
                    col(Request.id),
                    col(Request.response_status_code),
                    col(Request.response_url),
                    col(Request.content_size_original),
                    col(Request.content_size_compressed),
                    col(Request.continuation),
                    col(Request.response_created_at),
                    col(Request.compression_dict_id),
                    col(Request.speculation_outcome),
                ).where(
                    col(Request.id) == request_id,
                    col(Request.response_status_code).isnot(None),
                )
            )
            row = result.first()
            if row is None:
                return None
            return ResponseRecord(
                id=row[0],
                status_code=row[1],
                url=row[2],
                content_size_original=row[3],
                content_size_compressed=row[4],
                continuation=row[5],
                created_at=row[6],
                compression_dict_id=row[7],
                speculation_outcome=row[8],
            )

    async def get_result(self, result_id: int) -> ResultRecord | None:
        """Get a single result by ID.

        Args:
            result_id: The database ID of the result.

        Returns:
            ResultRecord or None if not found.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(
                    col(Result.id),
                    col(Result.request_id),
                    col(Result.result_type),
                    col(Result.data_json),
                    col(Result.is_valid),
                    col(Result.validation_errors_json),
                    col(Result.created_at),
                ).where(col(Result.id) == result_id)
            )
            row = result.first()
            if row is None:
                return None
            return ResultRecord(
                id=row[0],
                request_id=row[1],
                result_type=row[2],
                data_json=row[3],
                is_valid=bool(row[4]),
                validation_errors_json=row[5],
                created_at=row[6],
            )

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

    async def get_predicate_result(self, request_id: int) -> bool:
        """Get predicate_result from a resume request's permanent_json.

        Args:
            request_id: The database ID of the resume request.

        Returns:
            The predicate_result boolean value.
        """
        permanent_json = await self.get_permanent_json(request_id)
        if permanent_json:
            data = json.loads(permanent_json)
            return data.get("predicate_result", False)
        return False

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
            self._session_factory, compressed, dict_id
        )

    async def get_response_content_with_headers(
        self, request_id: int
    ) -> tuple[bytes, str | None] | None:
        """Get decompressed response content and headers.

        Args:
            request_id: The database ID of the request.

        Returns:
            Tuple of (decompressed_content, headers_json) or None if not found.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(
                    col(Request.content_compressed),
                    col(Request.compression_dict_id),
                    col(Request.response_headers_json),
                ).where(
                    col(Request.id) == request_id,
                    col(Request.response_status_code).isnot(None),
                )
            )
            row = result.first()

        if row is None:
            return None

        compressed_content, dict_id, headers_json = row

        if compressed_content is None:
            return (b"", headers_json)

        content = await decompress_response(
            self._session_factory, compressed_content, dict_id
        )
        return (content, headers_json)
