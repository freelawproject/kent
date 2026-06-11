"""Tests for request queue operations (_requests.py)."""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

from jkent.driver.database_engine.queue import RequestQueueDB
from jkent.driver.database_engine.sql_manager import SQLManager


class TestRequestOperations:
    """Tests for request queue operations."""

    async def test_insert_request(self, sql_manager: SQLManager) -> None:
        """Test inserting a new request."""
        request_id = await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/page",
            headers_json=json.dumps({"Accept": "text/html"}),
            cookies_json=None,
            body=None,
            continuation="parse",
            current_location="https://example.com",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key="GET:https://example.com/page",
            parent_id=None,
        )

        assert request_id > 0

        # Verify request was inserted
        async with sql_manager._session_factory() as session:
            result = await session.execute(
                sa.text(
                    "SELECT url, method, status FROM requests WHERE id = :id"
                ),
                {"id": request_id},
            )
            row = result.first()
        assert row is not None
        assert row[0] == "https://example.com/page"
        assert row[1] == "GET"
        assert row[2] == "pending"

    async def test_check_dedup_key_exists(
        self, sql_manager: SQLManager
    ) -> None:
        """Test deduplication key checking."""
        dedup_key = "GET:https://example.com/unique"

        # Should not exist initially
        assert not await sql_manager.check_dedup_key_exists(dedup_key)

        # Insert request with dedup key
        await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/unique",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key=dedup_key,
            parent_id=None,
        )

        # Should exist now
        assert await sql_manager.check_dedup_key_exists(dedup_key)

    async def test_get_next_pending_request(
        self, sql_manager: SQLManager
    ) -> None:
        """Test getting next pending request from queue."""
        # Insert requests with different priorities
        await sql_manager.insert_request(
            priority=10,  # Lower priority (higher number)
            request_type="navigating",
            method="GET",
            url="https://example.com/low-priority",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key="low",
            parent_id=None,
        )

        await sql_manager.insert_request(
            priority=1,  # Higher priority (lower number)
            request_type="navigating",
            method="GET",
            url="https://example.com/high-priority",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key="high",
            parent_id=None,
        )

        row = await sql_manager.get_next_pending_request()

        assert row is not None
        # Should get high priority request first (priority=1)
        # Column order: id, request_type, method, url, headers_json, ...
        assert row[3] == "https://example.com/high-priority"

    async def test_mark_request_in_progress(
        self, sql_manager: SQLManager
    ) -> None:
        """Test marking a request as in progress."""
        request_id = await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/test",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key=None,
            parent_id=None,
        )

        await sql_manager.mark_request_in_progress(request_id)

        async with sql_manager._session_factory() as session:
            result = await session.execute(
                sa.text(
                    "SELECT status, started_at FROM requests WHERE id = :id"
                ),
                {"id": request_id},
            )
            row = result.first()
        assert row is not None
        assert row[0] == "in_progress"
        assert row[1] is not None  # started_at should be set

    async def test_mark_request_completed(
        self, sql_manager: SQLManager
    ) -> None:
        """Test marking a request as completed."""
        request_id = await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/test",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key=None,
            parent_id=None,
        )

        await sql_manager.mark_request_in_progress(request_id)
        await sql_manager.mark_request_completed(request_id)

        async with sql_manager._session_factory() as session:
            result = await session.execute(
                sa.text(
                    "SELECT status, completed_at FROM requests WHERE id = :id"
                ),
                {"id": request_id},
            )
            row = result.first()
        assert row is not None
        assert row[0] == "completed"
        assert row[1] is not None  # completed_at should be set

    async def test_mark_request_failed(self, sql_manager: SQLManager) -> None:
        """Test marking a request as failed."""
        request_id = await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/test",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key=None,
            parent_id=None,
        )

        await sql_manager.mark_request_failed(request_id, "Test error")

        async with sql_manager._session_factory() as session:
            result = await session.execute(
                sa.text(
                    "SELECT status, last_error FROM requests WHERE id = :id"
                ),
                {"id": request_id},
            )
            row = result.first()
        assert row is not None
        assert row[0] == "failed"
        assert row[1] == "Test error"

    async def test_restore_queue(self, sql_manager: SQLManager) -> None:
        """Test restore_queue resets in_progress to pending."""
        # Insert and mark a request as in_progress
        request_id = await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/test",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key=None,
            parent_id=None,
        )
        await sql_manager.mark_request_in_progress(request_id)

        # Verify it's in_progress
        async with sql_manager._session_factory() as session:
            result = await session.execute(
                sa.text("SELECT status FROM requests WHERE id = :id"),
                {"id": request_id},
            )
            row = result.first()
        assert row is not None
        assert row[0] == "in_progress"

        # Restore queue
        count = await sql_manager.restore_queue()

        # Should be back to pending
        async with sql_manager._session_factory() as session:
            result = await session.execute(
                sa.text("SELECT status FROM requests WHERE id = :id"),
                {"id": request_id},
            )
            row = result.first()
        assert row is not None
        assert row[0] == "pending"
        assert count == 1

    async def test_count_methods(self, sql_manager: SQLManager) -> None:
        """Test various count methods."""
        # Initially empty
        assert await sql_manager.count_pending_requests() == 0
        assert await sql_manager.count_active_requests() == 0
        assert await sql_manager.count_all_requests() == 0

        # Insert pending request
        req1 = await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/1",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key="1",
            parent_id=None,
        )

        assert await sql_manager.count_pending_requests() == 1
        assert await sql_manager.count_active_requests() == 1

        # Mark in progress
        await sql_manager.mark_request_in_progress(req1)

        assert await sql_manager.count_pending_requests() == 0
        assert await sql_manager.count_active_requests() == 1

        # Mark completed
        await sql_manager.mark_request_completed(req1)

        assert await sql_manager.count_pending_requests() == 0
        assert await sql_manager.count_active_requests() == 0
        assert await sql_manager.count_all_requests() == 1


class TestStepControl:
    """Tests for pause/resume step operations."""

    async def test_pause_step(self, sql_manager: SQLManager) -> None:
        """Test pausing requests for a continuation."""
        # Insert requests with different continuations
        await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/1",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse_listing",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key="1",
            parent_id=None,
        )
        await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/2",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse_listing",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key="2",
            parent_id=None,
        )
        await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/3",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse_detail",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key="3",
            parent_id=None,
        )

        # Pause parse_listing
        held_count = await sql_manager.pause_step("parse_listing")
        assert held_count == 2

        # Verify held count
        assert await sql_manager.get_held_count("parse_listing") == 2
        assert await sql_manager.get_held_count("parse_detail") == 0
        assert await sql_manager.get_held_count() == 2

    async def test_resume_step(self, sql_manager: SQLManager) -> None:
        """Test resuming held requests."""
        # Insert and pause
        await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/1",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key="1",
            parent_id=None,
        )

        await sql_manager.pause_step("parse")
        assert await sql_manager.get_held_count() == 1

        # Resume
        resumed_count = await sql_manager.resume_step("parse")
        assert resumed_count == 1
        assert await sql_manager.get_held_count() == 0


class TestCancelRequests:
    """Tests for request cancellation."""

    async def test_cancel_request(self, sql_manager: SQLManager) -> None:
        """Test cancelling a single pending request."""
        request_id = await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/test",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key=None,
            parent_id=None,
        )

        cancelled = await sql_manager.cancel_request(request_id)
        assert cancelled

        async with sql_manager._session_factory() as session:
            result = await session.execute(
                sa.text(
                    "SELECT status, last_error FROM requests WHERE id = :id"
                ),
                {"id": request_id},
            )
            row = result.first()
        assert row is not None
        assert row[0] == "failed"
        assert "Cancelled" in row[1]

    async def test_cancel_request_not_pending(
        self, sql_manager: SQLManager
    ) -> None:
        """Test that completed requests can't be cancelled."""
        request_id = await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/test",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key=None,
            parent_id=None,
        )
        await sql_manager.mark_request_in_progress(request_id)
        await sql_manager.mark_request_completed(request_id)

        cancelled = await sql_manager.cancel_request(request_id)
        assert not cancelled

    async def test_cancel_requests_by_continuation(
        self, sql_manager: SQLManager
    ) -> None:
        """Test batch cancelling requests by continuation."""
        # Create multiple requests
        await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/1",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key="1",
            parent_id=None,
        )
        await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/2",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key="2",
            parent_id=None,
        )
        await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/3",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="other",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key="3",
            parent_id=None,
        )

        count = await sql_manager.cancel_requests_by_continuation("parse")
        assert count == 2

        # Verify 'other' is still pending
        async with sql_manager._session_factory() as session:
            result = await session.execute(
                sa.text(
                    "SELECT status FROM requests WHERE continuation = 'other'"
                )
            )
            row = result.first()
        assert row is not None
        assert row[0] == "pending"  # type: ignore[index]


class TestAvgCompletedRequestDuration:
    """Tests for avg_completed_request_duration_s()."""

    async def test_no_completed_requests(
        self, sql_manager: SQLManager
    ) -> None:
        """Returns None when no completed requests exist."""
        result = await sql_manager.avg_completed_request_duration_s()
        assert result is None

    async def test_completed_requests_with_timestamps(
        self, sql_manager: SQLManager
    ) -> None:
        """Returns a positive float when completed requests have ns timestamps."""
        request_id = await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/test",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key=None,
            parent_id=None,
        )

        # dequeue sets started_at_ns, mark_completed sets completed_at_ns
        await sql_manager.dequeue_next_request()
        await sql_manager.mark_request_completed(request_id)

        result = await sql_manager.avg_completed_request_duration_s()
        assert result is not None
        assert result >= 0

    async def test_sample_size_limits_rows(
        self, sql_manager: SQLManager
    ) -> None:
        """sample_size parameter limits which rows are averaged."""
        for i in range(5):
            req_id = await sql_manager.insert_request(
                priority=5,
                request_type="navigating",
                method="GET",
                url=f"https://example.com/{i}",
                headers_json=None,
                cookies_json=None,
                body=None,
                continuation="parse",
                current_location="",
                accumulated_data_json=None,
                permanent_json=None,
                expected_type=None,
                dedup_key=f"key-{i}",
                parent_id=None,
            )
            await sql_manager.dequeue_next_request()
            await sql_manager.mark_request_completed(req_id)

        # Should work with a smaller sample size
        result = await sql_manager.avg_completed_request_duration_s(
            sample_size=2
        )
        assert result is not None
        assert result >= 0


class TestContinuationsNeedingCompressionDict:
    """Tests for continuations_needing_compression_dict()."""

    async def _insert_with_response(
        self,
        sql_manager: SQLManager,
        url: str,
        continuation: str,
        dedup_key: str,
        *,
        dict_id: int | None = None,
    ) -> int:
        """Helper: insert a request and stamp it with a response."""
        req_id = await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url=url,
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation=continuation,
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key=dedup_key,
            parent_id=None,
        )
        async with sql_manager._session_factory() as session:
            await session.execute(
                sa.text(
                    "UPDATE requests SET "
                    "  response_status_code = 200, "
                    "  content_compressed = X'00', "
                    "  compression_dict_id = :dict_id "
                    "WHERE id = :id"
                ),
                {"id": req_id, "dict_id": dict_id},
            )
            await session.commit()
        return req_id

    async def test_empty_db(self, sql_manager: SQLManager) -> None:
        """Returns empty list when no requests exist."""
        result = await sql_manager.continuations_needing_compression_dict()
        assert result == []

    async def test_below_threshold(self, sql_manager: SQLManager) -> None:
        """Continuation with fewer than threshold responses is not returned."""
        for i in range(5):
            await self._insert_with_response(
                sql_manager,
                f"https://example.com/{i}",
                "parse",
                f"k-{i}",
            )
        needing = await sql_manager.continuations_needing_compression_dict(
            threshold=10
        )
        assert needing == []

    async def test_at_threshold(self, sql_manager: SQLManager) -> None:
        """Continuation at threshold is returned."""
        for i in range(10):
            await self._insert_with_response(
                sql_manager,
                f"https://example.com/{i}",
                "parse",
                f"k-{i}",
            )
        result = await sql_manager.continuations_needing_compression_dict(
            threshold=10
        )
        assert result == ["parse"]

    async def test_dict_compressed_not_counted(
        self, sql_manager: SQLManager
    ) -> None:
        """Responses with a compression_dict_id are excluded."""
        # Create a real compression dict row to satisfy the FK constraint.
        async with sql_manager._session_factory() as session:
            await session.execute(
                sa.text(
                    "INSERT INTO compression_dicts "
                    "(continuation, version, dictionary_data, sample_count) "
                    "VALUES ('parse', 1, X'00', 0)"
                )
            )
            await session.commit()
            result = await session.execute(
                sa.text("SELECT id FROM compression_dicts LIMIT 1")
            )
            real_dict_id = result.scalar_one()

        # 8 without dict, 5 with dict — only 8 count toward threshold
        for i in range(8):
            await self._insert_with_response(
                sql_manager,
                f"https://example.com/a{i}",
                "parse",
                f"a-{i}",
            )
        for i in range(5):
            await self._insert_with_response(
                sql_manager,
                f"https://example.com/b{i}",
                "parse",
                f"b-{i}",
                dict_id=real_dict_id,
            )
        needing = await sql_manager.continuations_needing_compression_dict(
            threshold=10
        )
        assert needing == []


class TestReseedableRoundTrip:
    """reseedable must survive insert -> dequeue -> deserialize (regression).

    The dequeue RETURNING clause and the queue deserializer are positionally
    coupled; reseedable was persisted on insert but previously dropped on the
    way back out, so every dequeued request reset it to None.
    """

    @pytest.mark.parametrize("value", [True, False, None])
    async def test_reseedable_round_trips(
        self, sql_manager: SQLManager, value: bool | None
    ) -> None:
        await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/reseedable",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key=None,
            parent_id=None,
            reseedable=value,
        )

        queue = RequestQueueDB()
        queue.db = sql_manager
        dequeued = await queue._get_next_request()

        assert dequeued is not None
        _request_id, request, _parent_id = dequeued
        assert request.reseedable is value
