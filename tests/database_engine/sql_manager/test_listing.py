"""Tests for listing and getter operations (_listing.py)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from jkent.driver.database_engine.compression import compress
from jkent.driver.database_engine.sql_manager import SQLManager

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    InsertRequest = Callable[..., Awaitable[int]]


class TestListingOperations:
    """Tests for list_requests, list_responses, list_results."""

    async def test_list_requests_by_status(
        self, sql_manager: SQLManager, insert_request: InsertRequest
    ) -> None:
        """Test listing requests filtered by status."""
        # Create requests with different statuses
        req1 = await insert_request(url="https://example.com/1", dedup_key="1")
        _req2 = await insert_request(
            url="https://example.com/2", dedup_key="2"
        )

        # Complete one
        await sql_manager.mark_request_in_progress(req1)
        await sql_manager.mark_request_completed(req1)

        # List pending
        pending_page = await sql_manager.list_requests(status="pending")
        assert pending_page.total == 1
        assert all(r.status == "pending" for r in pending_page.items)

        # List completed
        completed_page = await sql_manager.list_requests(status="completed")
        assert completed_page.total == 1
        assert all(r.status == "completed" for r in completed_page.items)

    async def test_list_requests_by_continuation(
        self, sql_manager: SQLManager, insert_request: InsertRequest
    ) -> None:
        """Test listing requests filtered by continuation."""
        await insert_request(
            url="https://example.com/1",
            continuation="parse_listing",
            dedup_key="1",
        )
        await insert_request(
            url="https://example.com/2",
            continuation="parse_detail",
            dedup_key="2",
        )

        listing_page = await sql_manager.list_requests(
            continuation="parse_listing"
        )
        assert listing_page.total == 1
        assert listing_page.items[0].continuation == "parse_listing"

    async def test_list_requests_pagination(
        self, sql_manager: SQLManager, insert_request: InsertRequest
    ) -> None:
        """Test pagination in list_requests."""
        # Create 10 requests
        for i in range(10):
            await insert_request(
                url=f"https://example.com/{i}", dedup_key=str(i)
            )

        # Get first page
        page1 = await sql_manager.list_requests(limit=3, offset=0)
        assert page1.total == 10
        assert len(page1.items) == 3
        assert page1.has_more

        # Get second page
        page2 = await sql_manager.list_requests(limit=3, offset=3)
        assert len(page2.items) == 3
        assert page2.offset == 3

        # Get last page
        page_last = await sql_manager.list_requests(limit=3, offset=9)
        assert len(page_last.items) == 1
        assert not page_last.has_more

    async def test_list_responses(
        self, sql_manager: SQLManager, insert_request: InsertRequest
    ) -> None:
        """Test listing responses with filters."""
        req1 = await insert_request(
            url="https://example.com/1",
            continuation="parse_listing",
            dedup_key="1",
        )
        req2 = await insert_request(
            url="https://example.com/2",
            continuation="parse_detail",
            dedup_key="2",
        )

        # Store responses
        content = b"Content"
        compressed = compress(content)

        await sql_manager.store_response(
            request_id=req1,
            status_code=200,
            headers_json=None,
            url="https://example.com/1",
            compressed_content=compressed,
            content_size_original=len(content),
            content_size_compressed=len(compressed),
            dict_id=None,
            continuation="parse_listing",
        )
        await sql_manager.store_response(
            request_id=req2,
            status_code=200,
            headers_json=None,
            url="https://example.com/2",
            compressed_content=compressed,
            content_size_original=len(content),
            content_size_compressed=len(compressed),
            dict_id=None,
            continuation="parse_detail",
        )

        # Filter by continuation
        listing_page = await sql_manager.list_responses(
            continuation="parse_listing"
        )
        assert listing_page.total == 1
        assert listing_page.items[0].continuation == "parse_listing"

        # Get all
        all_page = await sql_manager.list_responses()
        assert all_page.total == 2

    async def test_list_results(
        self, sql_manager: SQLManager, insert_request: InsertRequest
    ) -> None:
        """Test listing results with filters."""
        req_id = await insert_request()

        await sql_manager.store_result(
            request_id=req_id,
            result_type="CaseData",
            data_json=json.dumps({"id": 1}),
            is_valid=True,
        )
        await sql_manager.store_result(
            request_id=req_id,
            result_type="CaseData",
            data_json=json.dumps({"id": 2}),
            is_valid=False,
            validation_errors_json=json.dumps([{"error": "bad"}]),
        )
        await sql_manager.store_result(
            request_id=req_id,
            result_type="DocumentData",
            data_json=json.dumps({"id": 3}),
            is_valid=True,
        )

        # Filter by type
        case_results = await sql_manager.list_results(result_type="CaseData")
        assert case_results.total == 2

        # Filter by validity
        valid_results = await sql_manager.list_results(is_valid=True)
        assert valid_results.total == 2

        invalid_results = await sql_manager.list_results(is_valid=False)
        assert invalid_results.total == 1


class TestGetterMethods:
    """Tests for get_request, get_response, get_result."""

    async def test_get_request_found(
        self, sql_manager: SQLManager, insert_request: InsertRequest
    ) -> None:
        """Test getting a request by ID when found."""
        request_id = await insert_request(
            current_location="https://example.com"
        )

        request = await sql_manager.get_request(request_id)

        assert request is not None
        assert request.id == request_id
        assert request.url == "https://example.com/test"
        assert request.method == "GET"
        assert request.continuation == "parse"

    async def test_get_request_not_found(
        self, sql_manager: SQLManager
    ) -> None:
        """Test getting a request by ID when not found."""
        request = await sql_manager.get_request(999)
        assert request is None

    async def test_get_response_found(
        self, sql_manager: SQLManager, insert_request: InsertRequest
    ) -> None:
        """Test getting a response by ID when found."""
        request_id = await insert_request()

        content = b"Test"
        compressed = compress(content)

        response_id = await sql_manager.store_response(
            request_id=request_id,
            status_code=200,
            headers_json=json.dumps({"Content-Type": "text/html"}),
            url="https://example.com/test",
            compressed_content=compressed,
            content_size_original=len(content),
            content_size_compressed=len(compressed),
            dict_id=None,
            continuation="parse",
        )

        response = await sql_manager.get_response(response_id)

        assert response is not None
        assert response.id == response_id
        assert response.status_code == 200
        assert response.url == "https://example.com/test"
        assert response.content_size_original == len(content)

    async def test_get_response_not_found(
        self, sql_manager: SQLManager
    ) -> None:
        """Test getting a response by ID when not found."""
        response = await sql_manager.get_response(999)
        assert response is None

    async def test_get_result_found(
        self, sql_manager: SQLManager, insert_request: InsertRequest
    ) -> None:
        """Test getting a result by ID when found."""
        request_id = await insert_request()

        result_id = await sql_manager.store_result(
            request_id=request_id,
            result_type="CaseData",
            data_json=json.dumps({"case_name": "Smith v. Jones", "id": 123}),
            is_valid=True,
        )

        result = await sql_manager.get_result(result_id)

        assert result is not None
        assert result.id == result_id
        assert result.result_type == "CaseData"
        assert result.is_valid
        data = json.loads(result.data_json)
        assert data["case_name"] == "Smith v. Jones"

    async def test_get_result_not_found(self, sql_manager: SQLManager) -> None:
        """Test getting a result by ID when not found."""
        result = await sql_manager.get_result(999)
        assert result is None


class TestRunStatus:
    """Tests for run status checking."""

    async def test_get_run_status_unstarted(
        self, sql_manager: SQLManager
    ) -> None:
        """Test run status is unstarted when no requests."""
        status = await sql_manager.get_run_status()
        assert status == "unstarted"

    async def test_get_run_status_in_progress(
        self, sql_manager: SQLManager, insert_request: InsertRequest
    ) -> None:
        """Test run status is in_progress with pending requests."""
        await insert_request()

        status = await sql_manager.get_run_status()
        assert status == "in_progress"

    async def test_get_run_status_done(
        self, sql_manager: SQLManager, insert_request: InsertRequest
    ) -> None:
        """Test run status is done when all completed."""
        request_id = await insert_request()
        await sql_manager.mark_request_in_progress(request_id)
        await sql_manager.mark_request_completed(request_id)

        status = await sql_manager.get_run_status()
        assert status == "done"
