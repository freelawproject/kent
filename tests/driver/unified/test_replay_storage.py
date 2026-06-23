"""Unit tests for ReplayStorage's terminal-state operations (LO-3).

These pin the branches the replay differential doesn't reach: the recursive
descendant pruning in ``finalize_stubs`` (the differential's missed rows are
leaves) and the reseedable parent-walk. Rows are built directly in the
``requests`` table so the tree shape is explicit.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from jkent.driver.database_engine.sql_manager import SQLManager
from jkent.driver.unified_driver.persistence import ReplayStorage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def sql_manager(tmp_path: Path) -> AsyncIterator[SQLManager]:
    async with SQLManager.open(tmp_path / "test.db") as manager:
        yield manager


async def _insert(
    sql: SQLManager,
    rid: int,
    *,
    parent: int | None = None,
    status: str = "completed",
    reseedable: int = 0,
) -> int:
    """Insert one request row with an explicit id; return it."""
    async with sql._lock, sql._session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO requests "
                "(id, queue_counter, method, url, continuation, status, "
                " parent_request_id, reseedable) "
                "VALUES (:id, :id, 'GET', :url, 'parse', :status, "
                " :parent, :reseedable)"
            ),
            {
                "id": rid,
                "url": f"https://example.com/{rid}",
                "status": status,
                "parent": parent,
                "reseedable": reseedable,
            },
        )
        await session.commit()
    return rid


async def _statuses(sql: SQLManager) -> dict[int, str]:
    async with sql._session_factory() as session:
        rows = (
            await session.execute(text("SELECT id, status FROM requests"))
        ).all()
    return {r[0]: r[1] for r in rows}


async def test_finalize_stubs_prunes_descendants(
    sql_manager: SQLManager,
) -> None:
    """A stubbed row's descendants are deleted; the stub becomes pending."""
    await _insert(sql_manager, 1, status="stubbed")  # the stub anchor
    await _insert(sql_manager, 2, parent=1)  # child
    await _insert(sql_manager, 3, parent=2)  # grandchild
    await _insert(sql_manager, 9, status="completed")  # unrelated sibling root

    await ReplayStorage(sql_manager).finalize_stubs()

    statuses = await _statuses(sql_manager)
    assert statuses == {1: "pending", 9: "completed"}  # 2 & 3 pruned


async def test_stub_with_reseedable_walk_stops_at_anchor(
    sql_manager: SQLManager,
) -> None:
    """The walk stubs the nearest reseedable=True ancestor, not the failed leaf."""
    await _insert(sql_manager, 1, reseedable=1)  # the reseedable anchor
    await _insert(sql_manager, 2, parent=1, reseedable=0)
    await _insert(sql_manager, 3, parent=2, reseedable=0)  # the failed leaf

    await ReplayStorage(sql_manager).stub_with_reseedable_walk(3)

    statuses = await _statuses(sql_manager)
    assert statuses[1] == "stubbed"  # walked up to the anchor
    assert statuses[2] == "completed"
    assert statuses[3] == "completed"  # leaf untouched


async def test_stub_with_reseedable_walk_falls_back_to_root(
    sql_manager: SQLManager,
) -> None:
    """With no reseedable ancestor, the walk stubs the root."""
    await _insert(sql_manager, 1, reseedable=0)  # root
    await _insert(sql_manager, 2, parent=1, reseedable=0)  # failed leaf

    await ReplayStorage(sql_manager).stub_with_reseedable_walk(2)

    statuses = await _statuses(sql_manager)
    assert statuses[1] == "stubbed"
    assert statuses[2] == "completed"


async def test_delete_request_row(sql_manager: SQLManager) -> None:
    """delete_request_row removes the row entirely."""
    await _insert(sql_manager, 1)
    await ReplayStorage(sql_manager).delete_request_row(1)
    assert await _statuses(sql_manager) == {}


async def test_stub_drops_produced_outputs_but_keeps_response(
    sql_manager: SQLManager,
) -> None:
    """Stubbing a completed row deletes its produced outputs (results / archived
    files / estimates / errors) so a downstream live run can't duplicate them,
    while keeping the stored response so a downstream replay can re-serve it.
    """
    await _insert(sql_manager, 1, reseedable=1)
    async with sql_manager._lock, sql_manager._session_factory() as session:
        # A completed row carries a stored response and the outputs it produced.
        await session.execute(
            text(
                "UPDATE requests SET response_status_code = 200, "
                "content_compressed = :body, content_size_original = 5 "
                "WHERE id = 1"
            ),
            {"body": b"hello"},
        )
        await session.execute(
            text(
                "INSERT INTO results (request_id, result_type, data_json) "
                "VALUES (1, 'dict', '{\"a\": 1}')"
            )
        )
        await session.execute(
            text(
                "INSERT INTO archived_files "
                "(request_id, file_path, original_url) "
                "VALUES (1, '/tmp/f', 'https://example.com/1')"
            )
        )
        await session.execute(
            text(
                "INSERT INTO estimates "
                "(request_id, expected_types_json, min_count) "
                "VALUES (1, '[\"dict\"]', 1)"
            )
        )
        await session.execute(
            text(
                "INSERT INTO errors "
                "(request_id, error_type, error_class, message, request_url) "
                "VALUES (1, 'parse', 'ValueError', 'boom', "
                "'https://example.com/1')"
            )
        )
        await session.commit()

    await ReplayStorage(sql_manager).stub_request(1)

    async with sql_manager._session_factory() as session:
        counts = {
            table: (
                await session.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE request_id = 1")
                )
            ).scalar_one()
            for table in ("results", "archived_files", "estimates", "errors")
        }
        row = (
            await session.execute(
                text(
                    "SELECT status, response_status_code, content_compressed "
                    "FROM requests WHERE id = 1"
                )
            )
        ).first()

    assert counts == {
        "results": 0,
        "archived_files": 0,
        "estimates": 0,
        "errors": 0,
    }
    assert row is not None
    assert row[0] == "stubbed"
    assert row[1] == 200  # response retained
    assert row[2] == b"hello"  # content retained
