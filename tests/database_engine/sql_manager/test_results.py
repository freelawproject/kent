"""Tests for result storage operations (_results.py)."""

from __future__ import annotations

import json

import sqlalchemy as sa

from jkent.driver.database_engine.sql_manager import SQLManager


class TestResultStorage:
    """Tests for result storage operations."""

    async def test_store_result_valid(self, sql_manager: SQLManager) -> None:
        """Test storing a valid result."""
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

        result_id = await sql_manager.store_result(
            request_id=request_id,
            result_type="CaseData",
            data_json=json.dumps({"case_name": "Smith v. Jones", "id": 123}),
            is_valid=True,
            validation_errors_json=None,
        )

        assert result_id > 0

        # Verify result was stored
        async with sql_manager._session_factory() as session:
            result = await session.execute(
                sa.text(
                    "SELECT result_type, is_valid, data_json FROM results WHERE id = :id"
                ),
                {"id": result_id},
            )
            row = result.first()
        assert row is not None
        assert row[0] == "CaseData"
        assert row[1] == 1  # is_valid
        data = json.loads(row[2])
        assert data["case_name"] == "Smith v. Jones"

    async def test_store_result_invalid(self, sql_manager: SQLManager) -> None:
        """Test storing an invalid result with validation errors."""
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

        validation_errors = [
            {"loc": ["docket_number"], "msg": "field required"}
        ]

        result_id = await sql_manager.store_result(
            request_id=request_id,
            result_type="CaseData",
            data_json=json.dumps({"case_name": "Incomplete"}),
            is_valid=False,
            validation_errors_json=json.dumps(validation_errors),
        )

        # Verify result was stored as invalid
        async with sql_manager._session_factory() as session:
            result = await session.execute(
                sa.text(
                    "SELECT is_valid, validation_errors_json FROM results WHERE id = :id"
                ),
                {"id": result_id},
            )
            row = result.first()
        assert row is not None
        assert row[0] == 0  # is_valid = False
        assert row[1] is not None
