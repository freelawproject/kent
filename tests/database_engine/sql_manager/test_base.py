"""Tests for SQLManager context manager and initialization (_base.py)."""

from __future__ import annotations

from pathlib import Path

from jkent.driver.database_engine.sql_manager import SQLManager


class TestSQLManagerContext:
    """Tests for SQLManager context manager and initialization."""

    async def test_open_context_manager(self, db_path: Path) -> None:
        """Test SQLManager.open context manager creates and closes properly."""
        async with SQLManager.open(db_path) as manager:
            assert manager._engine is not None
            # Can perform operations
            stats = await manager.get_stats()
            assert stats is not None

    async def test_engine_property(self, sql_manager: SQLManager) -> None:
        """Test engine is set on the manager."""
        assert sql_manager._engine is not None
