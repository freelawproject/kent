"""Standalone types and utility functions for the sql_manager package.

Defines data transfer objects (Page, record dataclasses) and the
compute_cache_key helper used across the driver.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from typing import Any, Generic, TypeVar


def compute_cache_key(
    method: str,
    url: str,
    body: bytes | None = None,
    headers_json: str | None = None,
) -> str:
    """Compute a cache key for response caching.

    The cache key is a SHA256 hash of the request parameters that affect
    the response: method, URL, body, and headers.

    Args:
        method: HTTP method (GET, POST, etc.).
        url: Request URL.
        body: Request body bytes (for POST/PUT requests).
        headers_json: JSON-encoded headers (optional).

    Returns:
        Hex-encoded SHA256 hash string.
    """
    hasher = hashlib.sha256()
    hasher.update(method.encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update(url.encode("utf-8"))
    hasher.update(b"\x00")
    if body:
        hasher.update(body)
    hasher.update(b"\x00")
    if headers_json:
        hasher.update(headers_json.encode("utf-8"))
    return hasher.hexdigest()


def _compression_ratio(
    original: int | None, compressed: int | None
) -> float | None:
    """Ratio of original to compressed size, rounded to 2 dp.

    Returns None when either size is missing or zero (avoids div-by-zero
    and meaningless ratios).
    """
    if original and compressed:
        return round(original / compressed, 2)
    return None


T = TypeVar("T")


@dataclass
class Page(Generic[T]):
    """Paginated result set.

    Attributes:
        items: List of items for this page.
        total: Total number of items matching the query.
        offset: Number of items skipped.
        limit: Maximum items per page.
    """

    items: list[T]
    total: int
    offset: int
    limit: int

    @property
    def has_more(self) -> bool:
        """Check if there are more items after this page."""
        return self.offset + len(self.items) < self.total

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "items": [
                item.to_dict() if hasattr(item, "to_dict") else str(item)  # type: ignore
                for item in self.items
            ],
            "total": self.total,
            "offset": self.offset,
            "limit": self.limit,
            "has_more": self.has_more,
        }

    def to_json(self) -> str:
        """Serialize to JSON."""
        return json.dumps(self.to_dict())


@dataclass
class IncidentalRequestRecord:
    """Incidental request record joining metadata and storage tables.

    Represents a browser-initiated network request captured by Playwright,
    combining timing/metadata from incidental_requests with content/response
    data from incidental_request_storage.
    """

    id: int
    parent_request_id: int
    url: str
    headers_json: str | None
    started_at_ns: int | None
    completed_at_ns: int | None
    from_cache: bool | None
    created_at: str | None
    storage_id: int | None
    # Fields from storage table (joined)
    resource_type: str | None = None
    method: str | None = None
    status_code: int | None = None
    content_size_original: int | None = None
    content_size_compressed: int | None = None
    failure_reason: str | None = None

    @property
    def duration_ns(self) -> int | None:
        if self.started_at_ns is not None and self.completed_at_ns is not None:
            return self.completed_at_ns - self.started_at_ns
        return None

    @property
    def duration_ms(self) -> float | None:
        duration = self.duration_ns
        if duration is not None:
            return duration / 1_000_000
        return None

    @property
    def compression_ratio(self) -> float | None:
        return _compression_ratio(
            self.content_size_original, self.content_size_compressed
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_request_id": self.parent_request_id,
            "url": self.url,
            "headers_json": self.headers_json,
            "started_at_ns": self.started_at_ns,
            "completed_at_ns": self.completed_at_ns,
            "from_cache": self.from_cache,
            "created_at": self.created_at,
            "storage_id": self.storage_id,
            "resource_type": self.resource_type,
            "method": self.method,
            "status_code": self.status_code,
            "content_size_original": self.content_size_original,
            "content_size_compressed": self.content_size_compressed,
            "compression_ratio": self.compression_ratio,
            "failure_reason": self.failure_reason,
            "duration_ms": self.duration_ms,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


@dataclass
class RequestRecord:
    """Request record from database.

    Represents a row from the requests table with essential fields
    for display and inspection.
    """

    id: int
    status: str
    priority: int
    queue_counter: int
    method: str
    url: str
    continuation: str
    current_location: str
    created_at: str | None
    started_at: str | None
    completed_at: str | None
    retry_count: int
    cumulative_backoff: float | None
    last_error: str | None
    # High-precision monotonic timestamps (nanoseconds from time.monotonic_ns()).
    # NOTE: these come from a per-process monotonic clock with no fixed epoch.
    # Deltas between two stamps taken in the SAME process run (e.g. duration_ns,
    # set at dequeue and completion) are meaningful; deltas that can straddle a
    # restart (e.g. queue_time_ns, where created_at_ns is stamped at insert and
    # started_at_ns at a later dequeue) are not guaranteed across restarts and
    # are intended only as best-effort stats, not exact measurements.
    created_at_ns: int | None = None
    started_at_ns: int | None = None
    completed_at_ns: int | None = None

    @property
    def duration_ns(self) -> int | None:
        """Calculate request duration in nanoseconds (from started to completed)."""
        if self.started_at_ns is not None and self.completed_at_ns is not None:
            return self.completed_at_ns - self.started_at_ns
        return None

    @property
    def duration_ms(self) -> float | None:
        """Calculate request duration in milliseconds."""
        duration = self.duration_ns
        if duration is not None:
            return duration / 1_000_000
        return None

    @property
    def queue_time_ns(self) -> int | None:
        """Time spent in queue in nanoseconds (from created to started).

        Best-effort only: created_at_ns and started_at_ns may be stamped in
        different process runs (insert vs. a later dequeue), and the monotonic
        clock is not comparable across restarts, so this value is not
        guaranteed when a request was queued across a restart.
        """
        if self.created_at_ns is not None and self.started_at_ns is not None:
            return self.started_at_ns - self.created_at_ns
        return None

    @property
    def queue_time_ms(self) -> float | None:
        """Calculate time spent in queue in milliseconds."""
        queue_time = self.queue_time_ns
        if queue_time is not None:
            return queue_time / 1_000_000
        return None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "status": self.status,
            "priority": self.priority,
            "queue_counter": self.queue_counter,
            "method": self.method,
            "url": self.url,
            "continuation": self.continuation,
            "current_location": self.current_location,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "retry_count": self.retry_count,
            "cumulative_backoff": self.cumulative_backoff,
            "last_error": self.last_error,
            "created_at_ns": self.created_at_ns,
            "started_at_ns": self.started_at_ns,
            "completed_at_ns": self.completed_at_ns,
            "duration_ms": self.duration_ms,
            "queue_time_ms": self.queue_time_ms,
        }

    def to_json(self) -> str:
        """Serialize to JSON."""
        return json.dumps(self.to_dict())

    @classmethod
    def select_columns(cls, source: Any) -> tuple[Any, ...]:
        """Return the ordered columns to select for building a RequestRecord.

        ``source`` may be the Request model class or an aliased column
        collection (e.g. ``Request.__table__.alias("r").c``); both support
        attribute access by column name.
        """
        return tuple(getattr(source, f.name) for f in fields(cls))

    @classmethod
    def from_row(cls, row: Any) -> RequestRecord:
        """Build a RequestRecord from a row whose columns follow select_columns order."""
        return cls(**{f.name: row[i] for i, f in enumerate(fields(cls))})


@dataclass
class ResponseRecord:
    """Response projection from the requests table.

    Represents the response-related fields of a request row.
    Does not include compressed content.
    """

    id: int
    status_code: int
    url: str
    content_size_original: int | None
    content_size_compressed: int | None
    continuation: str
    created_at: str | None
    compression_dict_id: int | None
    speculation_outcome: str | None = None

    @property
    def compression_ratio(self) -> float | None:
        """Calculate compression ratio if sizes are available."""
        return _compression_ratio(
            self.content_size_original, self.content_size_compressed
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "status_code": self.status_code,
            "url": self.url,
            "content_size_original": self.content_size_original,
            "content_size_compressed": self.content_size_compressed,
            "compression_ratio": self.compression_ratio,
            "continuation": self.continuation,
            "created_at": self.created_at,
            "compression_dict_id": self.compression_dict_id,
            "speculation_outcome": self.speculation_outcome,
        }

    def to_json(self) -> str:
        """Serialize to JSON."""
        return json.dumps(self.to_dict())

    @classmethod
    def select_columns(cls, request: Any) -> tuple[Any, ...]:
        """Ordered Request columns to select for a ResponseRecord.

        ``request`` is the Request model class (or an aliased column
        collection). Field names differ from column names, so the mapping
        is explicit; column order must match :meth:`from_row`.
        """
        return (
            request.id,
            request.response_status_code,
            request.response_url,
            request.content_size_original,
            request.content_size_compressed,
            request.continuation,
            request.response_created_at,
            request.compression_dict_id,
            request.speculation_outcome,
        )

    @classmethod
    def from_row(cls, row: Any) -> ResponseRecord:
        """Build a ResponseRecord from a row following select_columns order."""
        return cls(
            id=row[0],
            status_code=row[1],
            url=row[2],
            content_size_original=row[3],
            content_size_compressed=row[4],
            continuation=row[5],
            created_at=row[6],
            compression_dict_id=row[7],
            speculation_outcome=row[8],
        )


@dataclass
class ResultRecord:
    """Result record from database.

    Represents a row from the results table with essential fields
    for display.
    """

    id: int
    request_id: int | None
    result_type: str
    data_json: str
    is_valid: bool
    validation_errors_json: str | None
    created_at: str | None

    @property
    def data(self) -> dict[str, Any] | None:
        """Parse and return the data as a dictionary."""
        if self.data_json:
            return json.loads(self.data_json)
        return None

    @property
    def validation_errors(self) -> list[str] | None:
        """Parse and return validation errors as a list."""
        if self.validation_errors_json:
            return json.loads(self.validation_errors_json)
        return None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "request_id": self.request_id,
            "result_type": self.result_type,
            "data": json.loads(self.data_json) if self.data_json else None,
            "is_valid": self.is_valid,
            "validation_errors": (
                json.loads(self.validation_errors_json)
                if self.validation_errors_json
                else None
            ),
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        """Serialize to JSON."""
        return json.dumps(self.to_dict())

    @classmethod
    def select_columns(cls, result: Any) -> tuple[Any, ...]:
        """Ordered Result columns to select for a ResultRecord.

        ``result`` is the Result model class (or an aliased column
        collection). Column order must match :meth:`from_row`.
        """
        return (
            result.id,
            result.request_id,
            result.result_type,
            result.data_json,
            result.is_valid,
            result.validation_errors_json,
            result.created_at,
        )

    @classmethod
    def from_row(cls, row: Any) -> ResultRecord:
        """Build a ResultRecord from a row following select_columns order."""
        return cls(
            id=row[0],
            request_id=row[1],
            result_type=row[2],
            data_json=row[3],
            is_valid=bool(row[4]),
            validation_errors_json=row[5],
            created_at=row[6],
        )
