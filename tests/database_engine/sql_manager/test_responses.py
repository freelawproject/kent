"""Tests for response storage operations (_responses.py)."""

from __future__ import annotations

import json

import sqlalchemy as sa

from jkent.driver.database_engine.compression import compress
from jkent.driver.database_engine.sql_manager import SQLManager


class TestResponseStorage:
    """Tests for response storage operations."""

    async def test_store_response(self, sql_manager: SQLManager) -> None:
        """Test storing an HTTP response."""
        # First create a request
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

        content = b"<html>Test content</html>"
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

        assert response_id == request_id

        # Verify response was stored
        async with sql_manager._session_factory() as session:
            result = await session.execute(
                sa.text(
                    "SELECT response_status_code, content_size_original FROM requests WHERE id = :id"
                ),
                {"id": request_id},
            )
            row = result.first()
        assert row is not None
        assert row[0] == 200
        assert row[1] == len(content)

    async def test_get_response_content(self, sql_manager: SQLManager) -> None:
        """Test retrieving decompressed response content."""
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

        content = b"<html>Test content for retrieval</html>"
        compressed = compress(content)

        await sql_manager.store_response(
            request_id=request_id,
            status_code=200,
            headers_json=None,
            url="https://example.com/test",
            compressed_content=compressed,
            content_size_original=len(content),
            content_size_compressed=len(compressed),
            dict_id=None,
            continuation="parse",
        )

        # Retrieve content
        retrieved = await sql_manager.get_response_content(request_id)

        assert retrieved == content

    async def test_get_response_content_empty(
        self, sql_manager: SQLManager
    ) -> None:
        """Test retrieving empty response content (headers only)."""
        request_id = await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="HEAD",
            url="https://example.com/resource",
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

        await sql_manager.store_response(
            request_id=request_id,
            status_code=200,
            headers_json=json.dumps(
                {"Content-Type": "application/pdf", "Content-Length": "5000"}
            ),
            url="https://example.com/resource",
            compressed_content=None,
            content_size_original=0,
            content_size_compressed=0,
            dict_id=None,
            continuation="parse",
        )

        # Retrieve content
        retrieved = await sql_manager.get_response_content(request_id)

        assert retrieved == b""
