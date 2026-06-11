from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from jkent.driver.database_engine.compression import compress
from jkent.driver.database_engine.database import init_database
from jkent.driver.database_engine.sql_manager import SQLManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

    from jkent.driver.database_engine.scoped_session import (
        ScopedSessionFactory,
    )

# What the initialized_db fixture resolves to for its consumers.
_InitializedDB = tuple["AsyncEngine", "ScopedSessionFactory"]


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    """Create a temporary database path."""
    return tmp_path / "test.db"


@pytest.fixture
async def initialized_db(db_path: Path) -> AsyncIterator[_InitializedDB]:
    """Create an initialized database engine and session factory."""
    engine, session_factory = await init_database(db_path)
    yield engine, session_factory
    await engine.dispose()


@pytest.fixture
async def sql_manager(initialized_db: _InitializedDB) -> SQLManager:
    """Create a SQLManager instance."""
    engine, session_factory = initialized_db
    return SQLManager(engine, session_factory)


@pytest.fixture
async def populated_db(initialized_db: _InitializedDB):
    """Create a populated database with sample data for testing.

    This fixture creates:
    - Run metadata for a test scraper
    - Multiple requests with various statuses
    - Responses with content
    - Results (both valid and invalid)
    - Errors (both resolved and unresolved)
    - Rate limiter state
    - Compression dictionaries
    """
    engine, session_factory = initialized_db
    sql_manager = SQLManager(engine, session_factory)

    # Insert run metadata directly (since we need fields not in init_run_metadata)
    async with session_factory() as session:
        await session.execute(
            sa.text(
                """
                INSERT INTO run_metadata (
                    scraper_name, scraper_version, status, created_at,
                    base_delay, jitter, num_workers, max_backoff_time, speculation_config_json
                ) VALUES (:scraper_name, :scraper_version, :status, datetime('now'), :base_delay, :jitter, :num_workers, :max_backoff_time, :speculation_config_json)
                """
            ),
            {
                "scraper_name": "test.scraper",
                "scraper_version": "1.0.0",
                "status": "running",
                "base_delay": 0.5,
                "jitter": 0.2,
                "num_workers": 4,
                "max_backoff_time": 300.0,
                "speculation_config_json": "{}",
            },
        )
        await session.commit()

    # Insert multiple requests with different statuses
    request_data = [
        ("GET", "https://example.com/page1", "step1", "pending"),
        ("GET", "https://example.com/page2", "step1", "completed"),
        ("GET", "https://example.com/page3", "step2", "failed"),
        ("GET", "https://example.com/page4", "step2", "held"),
        ("GET", "https://example.com/page5", "step1", "completed"),
    ]

    request_ids = []
    for method, url, continuation, target_status in request_data:
        # Insert using SQLManager
        request_id = await sql_manager.insert_request(
            priority=1,
            request_type="navigating",
            method=method,
            url=url,
            headers_json="{}",
            cookies_json="{}",
            body=None,
            continuation=continuation,
            current_location="",
            accumulated_data_json="{}",
            permanent_json="{}",
            expected_type=None,
            dedup_key=None,
            parent_id=None,
        )
        request_ids.append(request_id)

        # Update status to target status (since insert always creates pending)
        if target_status != "pending":
            async with session_factory() as session:
                await session.execute(
                    sa.text(
                        "UPDATE requests SET status = :status WHERE id = :id"
                    ),
                    {"status": target_status, "id": request_id},
                )
                await session.commit()

    # Insert responses for completed requests using SQLManager
    response_data = [
        (
            request_ids[1],
            200,
            b"<html>Response 1</html>",
            "step1",
            "https://example.com/page2",
        ),
        (
            request_ids[4],
            200,
            b"<html>Response 2</html>",
            "step1",
            "https://example.com/page5",
        ),
    ]

    response_ids = []
    for request_id, status_code, content, continuation, url in response_data:
        compressed_content = compress(content)
        response_id = await sql_manager.store_response(
            request_id=request_id,
            status_code=status_code,
            headers_json="{}",
            url=url,
            compressed_content=compressed_content,
            content_size_original=len(content),
            content_size_compressed=len(compressed_content),
            dict_id=None,
            continuation=continuation,
            speculation_outcome=None,
        )
        response_ids.append(response_id)

    # Insert results for completed responses (using raw SQL for test fixture)
    result_data = [
        (request_ids[1], "TestResult", {"title": "Result 1"}, True, None),
        (
            request_ids[4],
            "TestResult",
            {"title": "Result 2"},
            False,
            ["error1"],
        ),
    ]

    async with session_factory() as session:
        for request_id, result_type, data, is_valid, errors in result_data:
            await session.execute(
                sa.text(
                    """
                    INSERT INTO results (
                        request_id, result_type, data_json, is_valid,
                        validation_errors_json, created_at
                    ) VALUES (:request_id, :result_type, :data_json, :is_valid,
                        :validation_errors_json, datetime('now'))
                    """
                ),
                {
                    "request_id": request_id,
                    "result_type": result_type,
                    "data_json": json.dumps(data),
                    "is_valid": is_valid,
                    "validation_errors_json": json.dumps(errors)
                    if errors
                    else None,
                },
            )
        await session.commit()

    # Insert errors (using raw SQL for test fixture)
    error_data = [
        (
            request_ids[2],
            "xpath",
            "XPath not found",
            "//*[@id='test']",
            "xpath",
            1,
            1,
            0,
            False,
            None,
        ),
        (
            request_ids[3],
            "http",
            "Connection timeout",
            None,
            None,
            None,
            None,
            None,
            True,
            "Resolved manually",
        ),
    ]

    async with session_factory() as session:
        for (
            request_id,
            error_type,
            message,
            selector,
            selector_type,
            expected_min,
            expected_max,
            actual_count,
            is_resolved,
            resolution_notes,
        ) in error_data:
            await session.execute(
                sa.text(
                    """
                    INSERT INTO errors (
                        request_id, error_type, message, selector, selector_type,
                        expected_min, expected_max, actual_count, is_resolved,
                        resolution_notes, created_at, request_url, error_class, traceback
                    ) VALUES (:request_id, :error_type, :message, :selector, :selector_type,
                        :expected_min, :expected_max, :actual_count, :is_resolved,
                        :resolution_notes, datetime('now'), :request_url, :error_class, :traceback)
                    """
                ),
                {
                    "request_id": request_id,
                    "error_type": error_type,
                    "message": message,
                    "selector": selector,
                    "selector_type": selector_type,
                    "expected_min": expected_min,
                    "expected_max": expected_max,
                    "actual_count": actual_count,
                    "is_resolved": is_resolved,
                    "resolution_notes": resolution_notes,
                    "request_url": f"https://example.com/page{request_id}",
                    "error_class": "TestError",
                    "traceback": "fake traceback",
                },
            )
        await session.commit()

    # Insert compression dictionary (using raw SQL for test fixture)
    async with session_factory() as session:
        await session.execute(
            sa.text(
                """
                INSERT INTO compression_dicts (
                    continuation, version, sample_count, dictionary_data, created_at
                ) VALUES (:continuation, :version, :sample_count, :dictionary_data, datetime('now'))
                """
            ),
            {
                "continuation": "step1",
                "version": 1,
                "sample_count": 100,
                "dictionary_data": b"fake_dict_data",
            },
        )
        await session.commit()

    return engine, session_factory
