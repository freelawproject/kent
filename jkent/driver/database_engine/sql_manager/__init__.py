"""SQLManager - Database operations for LocalDevDriver.

This package provides a standalone class for all SQLite database operations,
enabling independent testing and programmatic inspection of the database
without requiring a full driver instance.

The SQLManager handles:
- Request queue operations (enqueue, dequeue, status updates)
- Response storage with compression
- Result storage with validation tracking
- Error tracking
- Run metadata management
- Speculative progress tracking
- Statistics and listing operations
"""

from jkent.driver.database_engine.sql_manager._base import SQLManagerBase
from jkent.driver.database_engine.sql_manager._estimates import (
    EstimateStorageMixin,
)
from jkent.driver.database_engine.sql_manager._incidental_requests import (
    IncidentalRequestStorageMixin,
)
from jkent.driver.database_engine.sql_manager._listing import ListingMixin
from jkent.driver.database_engine.sql_manager._requests import (
    RequestQueueMixin,
)
from jkent.driver.database_engine.sql_manager._responses import (
    ResponseStorageMixin,
)
from jkent.driver.database_engine.sql_manager._results import (
    ResultStorageMixin,
)
from jkent.driver.database_engine.sql_manager._run_metadata import (
    RunMetadataMixin,
)
from jkent.driver.database_engine.sql_manager._speculation import (
    SpeculationMixin,
)
from jkent.driver.database_engine.sql_manager._types import (
    IncidentalRequestRecord,
    Page,
    RequestRecord,
    ResponseRecord,
    ResultRecord,
    compute_cache_key,
)
from jkent.driver.database_engine.sql_manager._validation import (
    ValidationMixin,
)


class SQLManager(
    RunMetadataMixin,
    RequestQueueMixin,
    ResponseStorageMixin,
    IncidentalRequestStorageMixin,
    ResultStorageMixin,
    EstimateStorageMixin,
    SpeculationMixin,
    ValidationMixin,
    ListingMixin,
    SQLManagerBase,
):
    """Database manager for LocalDevDriver operations.

    Provides all database operations needed by the LocalDevDriver in a
    standalone class that can be used independently for testing, inspection,
    and programmatic access to the SQLite database.

    Example::

        # Standalone usage for inspection
        async with SQLManager.open(db_path) as manager:
            stats = await manager.get_stats()
            requests = await manager.list_requests(status="pending")

        # With existing engine/session factory (for driver integration)
        manager = SQLManager(engine, session_factory)
        await manager.store_response(request_id, response, continuation)
    """

    pass


__all__ = [
    "IncidentalRequestRecord",
    "Page",
    "RequestRecord",
    "ResponseRecord",
    "ResultRecord",
    "SQLManager",
    "compute_cache_key",
]
