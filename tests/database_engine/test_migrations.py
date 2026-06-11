"""Tests for the database migration system."""

from __future__ import annotations

from pathlib import Path

from jkent.driver.database_engine.database import SCHEMA_VERSION, init_database
from jkent.driver.database_engine.migrations import (
    BASELINE_VERSION,
    _scan_migrations,
    get_current_version,
    get_latest_version,
    migrate_to,
)


class TestVersioning:
    """The current schema is the baseline; there is no historical chain."""

    def test_latest_version_matches_schema_version(self) -> None:
        """get_latest_version() matches the exported SCHEMA_VERSION."""
        assert get_latest_version() == SCHEMA_VERSION

    def test_baseline_is_the_floor(self) -> None:
        """With no migration files, the latest version is the baseline."""
        assert get_latest_version() >= BASELINE_VERSION
        if not _scan_migrations():
            assert get_latest_version() == BASELINE_VERSION

    def test_migration_files_are_above_baseline(self) -> None:
        """Any future migration files target a version past the baseline."""
        for version in _scan_migrations():
            assert version > BASELINE_VERSION, (
                f"Migration file version {version} is not above the "
                f"baseline {BASELINE_VERSION}"
            )

    def test_steps_within_version_are_contiguous(self) -> None:
        """Step numbers within a version start at 1 and are contiguous."""
        for version, steps in _scan_migrations().items():
            step_nums = [s for s, _ext, _path in steps]
            assert step_nums == list(range(1, len(step_nums) + 1)), (
                f"Version {version} has non-contiguous steps: {step_nums}"
            )


class TestMigrationRunner:
    """Test the migration runner against a real database."""

    async def test_fresh_db_stamped_at_baseline(self, tmp_path: Path) -> None:
        """A fresh database is initialized at the baseline schema version."""
        db_path = tmp_path / "test.db"
        engine, _session_factory = await init_database(db_path)
        try:
            assert await get_current_version(engine) == SCHEMA_VERSION
            assert SCHEMA_VERSION == BASELINE_VERSION
        finally:
            await engine.dispose()

    async def test_migrate_idempotent(self, tmp_path: Path) -> None:
        """Running migrate_to again applies nothing and keeps the version."""
        db_path = tmp_path / "test.db"
        engine, _session_factory = await init_database(db_path)
        try:
            applied = await migrate_to(engine)
            assert applied == []
            assert await get_current_version(engine) == BASELINE_VERSION
        finally:
            await engine.dispose()
