"""Tests for record serialization and data types (_types.py)."""

from __future__ import annotations

import json

from jkent.driver.database_engine.sql_manager import (
    Page,
    RequestRecord,
    ResponseRecord,
    ResultRecord,
)


class TestRecordSerialization:
    """Tests for record serialization methods."""

    async def test_request_record_to_dict(self) -> None:
        """Test RequestRecord.to_dict() and to_json()."""
        record = RequestRecord(
            id=1,
            status="pending",
            priority=5,
            queue_counter=1,
            method="GET",
            url="https://example.com",
            continuation="parse",
            current_location="",
            created_at="2024-01-01",
            started_at=None,
            completed_at=None,
            retry_count=0,
            cumulative_backoff=0.0,
            last_error=None,
        )

        d = record.to_dict()
        assert d["id"] == 1
        assert d["status"] == "pending"
        assert d["url"] == "https://example.com"

        json_str = record.to_json()
        parsed = json.loads(json_str)
        assert parsed["id"] == 1

    async def test_response_record_to_dict(self) -> None:
        """Test ResponseRecord.to_dict() with compression_ratio."""
        record = ResponseRecord(
            id=1,
            status_code=200,
            url="https://example.com",
            content_size_original=1000,
            content_size_compressed=100,
            continuation="parse",
            created_at="2024-01-01",
            compression_dict_id=None,
        )

        d = record.to_dict()
        assert d["compression_ratio"] == 10.0

    async def test_result_record_to_dict(self) -> None:
        """Test ResultRecord.to_dict() parses JSON fields."""
        record = ResultRecord(
            id=1,
            request_id=1,
            result_type="CaseData",
            data_json='{"name": "test"}',
            is_valid=True,
            validation_errors_json=None,
            created_at="2024-01-01",
        )

        d = record.to_dict()
        assert d["data"] == {"name": "test"}
        assert d["validation_errors"] is None

    async def test_page_to_dict(self) -> None:
        """Test Page.to_dict() and to_json()."""
        record = RequestRecord(
            id=1,
            status="pending",
            priority=5,
            queue_counter=1,
            method="GET",
            url="https://example.com",
            continuation="parse",
            current_location="",
            created_at="2024-01-01",
            started_at=None,
            completed_at=None,
            retry_count=0,
            cumulative_backoff=0.0,
            last_error=None,
        )

        page = Page(items=[record], total=10, offset=0, limit=1)

        d = page.to_dict()
        assert d["total"] == 10
        assert d["has_more"] is True
        assert len(d["items"]) == 1

        json_str = page.to_json()
        parsed = json.loads(json_str)
        assert parsed["total"] == 10
