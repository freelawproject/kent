"""Tests for estimate storage operations (_estimates.py)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import sqlalchemy as sa

from jkent.driver.database_engine.sql_manager import SQLManager

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    InsertRequest = Callable[..., Awaitable[int]]


class TestEstimateStorage:
    """Tests for estimate storage operations."""

    async def test_store_estimate(
        self, sql_manager: SQLManager, insert_request: InsertRequest
    ) -> None:
        """Test storing an estimate."""
        request_id = await insert_request(
            url="https://example.com/search", continuation="parse_search"
        )

        estimate_id = await sql_manager.store_estimate(
            request_id=request_id,
            expected_types_json=json.dumps(["CaseData"]),
            min_count=10,
            max_count=10,
        )

        assert estimate_id > 0

        # Verify estimate was stored
        async with sql_manager._session_factory() as session:
            result = await session.execute(
                sa.text(
                    "SELECT request_id, expected_types_json, min_count, max_count "
                    "FROM estimates WHERE id = :id"
                ),
                {"id": estimate_id},
            )
            row = result.first()
        assert row is not None
        assert row[0] == request_id
        assert json.loads(row[1]) == ["CaseData"]
        assert row[2] == 10
        assert row[3] == 10

    async def test_store_estimate_unbounded_max(
        self, sql_manager: SQLManager, insert_request: InsertRequest
    ) -> None:
        """Test storing an estimate with no max_count."""
        request_id = await insert_request(
            url="https://example.com/search", continuation="parse_search"
        )

        estimate_id = await sql_manager.store_estimate(
            request_id=request_id,
            expected_types_json=json.dumps(["CaseData", "DocumentData"]),
            min_count=100,
            max_count=None,
        )

        async with sql_manager._session_factory() as session:
            result = await session.execute(
                sa.text(
                    "SELECT expected_types_json, min_count, max_count "
                    "FROM estimates WHERE id = :id"
                ),
                {"id": estimate_id},
            )
            row = result.first()
        assert row is not None
        assert json.loads(row[0]) == ["CaseData", "DocumentData"]
        assert row[1] == 100
        assert row[2] is None
