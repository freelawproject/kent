"""Tests for ScopedSessionFactory and scoped session lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from jkent.driver.database_engine.database import init_database
from jkent.driver.database_engine.scoped_session import (
    ScopedSessionFactory,
    clear_scope,
    get_scope,
    set_scope,
)


@pytest.fixture
async def scoped_factory(
    tmp_path: Path,
) -> AsyncGenerator[tuple[ScopedSessionFactory, Any]]:  # type: ignore
    """Create a ScopedSessionFactory backed by a real SQLite database."""
    engine, factory = await init_database(tmp_path / "test.db")
    yield factory, engine  # type: ignore
    await factory.remove_all()
    await engine.dispose()


class TestScopeHelpers:
    """Tests for set_scope / clear_scope / get_scope."""

    def test_default_scope_is_none(self) -> None:
        assert get_scope() is None

    def test_set_and_get_scope(self) -> None:
        set_scope("worker-0")
        assert get_scope() == "worker-0"
        clear_scope()
        assert get_scope() is None

    def test_clear_scope(self) -> None:
        set_scope("x")
        clear_scope()
        assert get_scope() is None


class TestUnscopedBehavior:
    """When no scope is set, factory behaves like async_sessionmaker."""

    @pytest.mark.asyncio
    async def test_unscoped_returns_different_sessions(
        self, scoped_factory: tuple
    ) -> None:
        factory, _ = scoped_factory
        clear_scope()

        async with factory() as session_a:
            id_a = id(session_a)
        async with factory() as session_b:
            id_b = id(session_b)

        assert id_a != id_b, "Unscoped calls should return different sessions"

    @pytest.mark.asyncio
    async def test_unscoped_session_not_cached_in_registry(
        self, scoped_factory: tuple
    ) -> None:
        """Unscoped sessions are not stored in the registry."""
        factory, _ = scoped_factory
        clear_scope()

        async with factory() as session:
            await session.execute(sa.text("SELECT 1"))

        assert len(factory._registry) == 0


class TestScopedBehavior:
    """When a scope is set, factory returns the same cached session."""

    @pytest.mark.asyncio
    async def test_scoped_returns_same_session(
        self, scoped_factory: tuple
    ) -> None:
        factory, _ = scoped_factory
        set_scope("worker-0")

        try:
            async with factory() as session_a:
                id_a = id(session_a)
            async with factory() as session_b:
                id_b = id(session_b)

            assert id_a == id_b, "Scoped calls should return the same session"
        finally:
            await factory.remove("worker-0")
            clear_scope()

    @pytest.mark.asyncio
    async def test_scoped_session_not_closed_on_exit(
        self, scoped_factory: tuple
    ) -> None:
        factory, _ = scoped_factory
        set_scope("worker-0")

        try:
            async with factory() as session:
                await session.execute(sa.text("SELECT 1"))
                await session.commit()

            # Session should still be active after exiting the block
            assert session.is_active
        finally:
            await factory.remove("worker-0")
            clear_scope()

    @pytest.mark.asyncio
    async def test_scoped_session_persists_across_operations(
        self, scoped_factory: tuple
    ) -> None:
        """Multiple operations in the same scope reuse the same session."""
        factory, _ = scoped_factory
        set_scope("worker-0")

        try:
            # First operation
            async with factory() as session:
                await session.execute(sa.text("SELECT 1"))
                await session.commit()

            # Second operation — same session
            async with factory() as session2:
                await session2.execute(sa.text("SELECT 1"))
                await session2.commit()

            assert session is session2
        finally:
            await factory.remove("worker-0")
            clear_scope()


class TestScopeIsolation:
    """Different scope keys get different sessions."""

    @pytest.mark.asyncio
    async def test_different_scopes_get_different_sessions(
        self, scoped_factory: tuple
    ) -> None:
        factory, _ = scoped_factory

        set_scope("worker-0")
        async with factory() as session_0:
            id_0 = id(session_0)

        set_scope("worker-1")
        async with factory() as session_1:
            id_1 = id(session_1)

        assert id_0 != id_1

        await factory.remove("worker-0")
        await factory.remove("worker-1")
        clear_scope()

    @pytest.mark.asyncio
    async def test_contextvar_isolation_across_tasks(
        self, scoped_factory: tuple
    ) -> None:
        """Different asyncio tasks with different scope keys get different sessions."""
        factory, _ = scoped_factory
        results: dict[str, int] = {}

        async def worker(name: str) -> None:
            set_scope(name)
            async with factory() as session:
                results[name] = id(session)

        t0 = asyncio.create_task(worker("task-0"))
        t1 = asyncio.create_task(worker("task-1"))
        await asyncio.gather(t0, t1)

        assert results["task-0"] != results["task-1"]

        await factory.remove("task-0")
        await factory.remove("task-1")


class TestRollbackOnException:
    """Scoped session is rolled back (not closed) when an exception occurs."""

    @pytest.mark.asyncio
    async def test_rollback_on_exception_keeps_session_alive(
        self, scoped_factory: tuple
    ) -> None:
        factory, _ = scoped_factory
        set_scope("worker-0")

        try:
            # First block raises
            with pytest.raises(ValueError):
                async with factory() as session:
                    await session.execute(sa.text("SELECT 1"))
                    raise ValueError("boom")

            # Session should still be usable for the next operation
            async with factory() as session2:
                result = await session2.execute(sa.text("SELECT 1"))
                await session2.commit()
                assert result.scalar() == 1

            assert session is session2
        finally:
            await factory.remove("worker-0")
            clear_scope()


class TestRemove:
    """Tests for remove() and remove_all()."""

    @pytest.mark.asyncio
    async def test_remove_clears_registry(self, scoped_factory: tuple) -> None:
        factory, _ = scoped_factory
        set_scope("worker-0")

        async with factory() as session:
            await session.execute(sa.text("SELECT 1"))
            await session.commit()

        assert "worker-0" in factory._registry
        await factory.remove("worker-0")
        clear_scope()

        assert "worker-0" not in factory._registry

    @pytest.mark.asyncio
    async def test_remove_nonexistent_key_is_noop(
        self, scoped_factory: tuple
    ) -> None:
        factory, _ = scoped_factory
        await factory.remove("does-not-exist")  # Should not raise

    @pytest.mark.asyncio
    async def test_remove_all_clears_registry(
        self, scoped_factory: tuple
    ) -> None:
        factory, _ = scoped_factory

        for i in range(3):
            set_scope(f"worker-{i}")
            async with factory() as session:
                await session.execute(sa.text("SELECT 1"))
                await session.commit()

        assert len(factory._registry) == 3

        clear_scope()
        await factory.remove_all()

        assert len(factory._registry) == 0

    @pytest.mark.asyncio
    async def test_new_session_after_remove(
        self, scoped_factory: tuple
    ) -> None:
        """After removing a scope, a new session is created for that scope."""
        factory, _ = scoped_factory
        set_scope("worker-0")

        try:
            async with factory() as session_a:
                id_a = id(session_a)

            await factory.remove("worker-0")

            async with factory() as session_b:
                id_b = id(session_b)

            assert id_a != id_b, (
                "After remove, a fresh session should be created"
            )
        finally:
            await factory.remove("worker-0")
            clear_scope()


class TestDataIntegrity:
    """Verify that scoped sessions don't lose data across operations."""

    @pytest.mark.asyncio
    async def test_committed_data_persists_across_scoped_operations(
        self, scoped_factory: tuple
    ) -> None:
        """Data committed in one scoped block is visible in the next."""
        factory, _ = scoped_factory
        set_scope("worker-0")

        try:
            # Write data
            async with factory() as session:
                await session.execute(
                    sa.text("INSERT INTO schema_info (version) VALUES (:v)"),
                    {"v": 999},
                )
                await session.commit()

            # Read it back in a new block (same scoped session)
            async with factory() as session:
                result = await session.execute(
                    sa.text(
                        "SELECT version FROM schema_info WHERE version = 999"
                    )
                )
                row = result.first()
                assert row is not None
                assert row[0] == 999
        finally:
            await factory.remove("worker-0")
            clear_scope()
