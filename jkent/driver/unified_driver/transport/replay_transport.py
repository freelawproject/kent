"""Replay transport — resolves requests from a previous run's source DB(s).

v1 of the unified-driver replay backend. It looks a request up by its
deduplication key (URL + params + body), with a URL+body fallback for rows
whose key was NULL, and returns the stored response (or streams a stored
archive file).

It uses ``SourceIndex`` (from ``jkent.driver.replay``) for the lookup. Replay
does no network I/O, so it never rate-limits and holds no per-worker resource.

It also owns the replay-run execution-layer state that has nowhere else to
live: the mode-aware index build (the three :data:`MatchMode` variants),
scraper-class enforcement, and the cross-worker *retry-eligible parent* set
(``ReplayWorker`` consults it to force-miss children of a retried parent).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

from jkent.data_types import Response
from jkent.driver.replay.error_pruning import compute_pruning_plan
from jkent.driver.replay.errors import ReplayScraperMismatchError
from jkent.driver.replay.source_index import (
    SourceIndex,
    fallback_replay_key_for_request,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Sequence
    from pathlib import Path

    from jkent.data_types import BaseScraper, Request
    from jkent.driver.unified_driver.transport import (
        ArchiveStream,
        AwaitCondition,
        QueuedRequest,
    )

MatchMode = Literal["prev-error-free", "curr-error-free", "desc-error-free"]


class ReplayMiss(Exception):
    """No stored response matched the request."""

    def __init__(self, *, dedup_key: str | None, url: str) -> None:
        super().__init__(
            f"no stored response for url={url!r} (dedup_key={dedup_key!r})"
        )
        self.dedup_key = dedup_key
        self.url = url


class _NoopHandle:
    """Replay holds no per-worker resource."""

    async def reset_for_reuse(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _FileArchiveStream:
    """An ``ArchiveStream`` that reads a stored archive file in chunks."""

    def __init__(
        self,
        *,
        status_code: int,
        headers: dict[str, str],
        url: str,
        file_path: str,
        chunk_size: int = 65536,
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self.url = url
        # Public: replay references the source file verbatim (no copy), so the
        # ReplayWorker reads this path directly instead of streaming a copy.
        self.file_path = file_path
        self._chunk_size = chunk_size

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._chunks()

    async def _chunks(self) -> AsyncIterator[bytes]:
        with open(self.file_path, "rb") as handle:
            while True:
                chunk = await asyncio.to_thread(handle.read, self._chunk_size)
                if not chunk:
                    break
                yield chunk


def _dedup_key_of(request: Request) -> str | None:
    """The request's own dedup key, or None to fall through to the fallback.

    A ``str`` is neither ``None`` nor ``SkipDeduplicationCheck``, so the
    isinstance check covers all three cases of ``deduplication_key``.
    """
    key = request.deduplication_key
    return key if isinstance(key, str) else None


def _decode(content: bytes, headers: dict[str, str]) -> str:
    """Decode body to text, honoring a charset in content-type if present."""
    charset = "utf-8"
    ctype = next(
        (v for k, v in headers.items() if k.lower() == "content-type"), ""
    )
    if "charset=" in ctype:
        charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    try:
        return content.decode(charset, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


class ReplayTransport:
    """A :class:`~jkent.driver.unified_driver.transport.Transport` over source DBs.

    Args:
        source_db_paths: Source DB paths in priority order.
        mode: Replay mode. ``curr-error-free`` (default) serves every stored
            row; ``prev-error-free`` drops retry-eligible (parser-errored)
            rows so they miss; ``desc-error-free`` excludes the reseedable-anchor
            subtrees of errored rows (computed by a pre-pass) and exposes the
            anchors via :meth:`seed_anchor_rows` for the run to re-seed.
        scraper: When provided, ``open`` enforces that every source DB was
            produced by this scraper class (raises
            :class:`ReplayScraperMismatchError`).
        index_db_path: Optional on-disk path for the routing index.
    """

    def __init__(
        self,
        source_db_paths: list[Path],
        *,
        mode: MatchMode = "curr-error-free",
        scraper: BaseScraper | None = None,
        index_db_path: Path | None = None,
    ) -> None:
        self._paths = list(source_db_paths)
        self._mode: MatchMode = mode
        self._scraper = scraper
        self._index_db_path = index_db_path
        self._index: SourceIndex | None = None
        self._handles: dict[int, _NoopHandle] = {}
        # request_ids that resolved against a retry_eligible source row, shared
        # across all workers (this transport is a single run-scoped instance).
        # ReplayWorker force-misses children of these unless trust is set.
        self._retry_eligible_parents: set[int] = set()
        # Mode-3 anchor seed rows, captured during open(); empty otherwise.
        self._anchor_rows: list[dict] = []

    async def open(self) -> None:
        # Built synchronously: SourceIndex's in-memory index connection is
        # thread-bound, so build/lookup/close must share one thread (the
        # per-source-DB connections used by fetch_response are cross-thread
        # safe). The build is fast for replay-sized DBs.
        self._index = self._build_index()
        self._enforce_scraper_class(self._index)

    def _build_index(self) -> SourceIndex:
        """Build the routing index for the configured mode."""
        if self._mode == "prev-error-free":
            return SourceIndex(
                source_db_paths=self._paths,
                index_db_path=self._index_db_path,
                exclude_retry_eligible=True,
            )
        if self._mode == "desc-error-free":
            return self._build_desc_error_free_index()
        return SourceIndex.build(
            self._paths, index_db_path=self._index_db_path
        )

    def _build_desc_error_free_index(self) -> SourceIndex:
        """Pre-pass: prune reseedable-anchor subtrees, capture anchors to re-seed."""
        scratch = SourceIndex(source_db_paths=self._paths, index_db_path=None)
        try:
            plan = compute_pruning_plan(scratch)
        finally:
            scratch.close()
        index = SourceIndex(
            source_db_paths=self._paths,
            index_db_path=self._index_db_path,
            excluded_request_ids=plan.excluded_request_ids,
        )
        self._anchor_rows = self._collect_anchor_rows(index, plan.anchors)
        return index

    @staticmethod
    def _collect_anchor_rows(
        index: SourceIndex, anchors: dict[int, list[tuple[int, int]]]
    ) -> list[dict]:
        """Fetch the entry-request kwargs for each mode-3 anchor row."""
        rows: list[dict] = []
        for db_idx, db_anchors in anchors.items():
            for anchor_id, _depth in db_anchors:
                row = index.fetch_entry_request_row(db_idx, anchor_id)
                if row is not None:
                    rows.append(dict(row))
        return rows

    def _enforce_scraper_class(self, index: SourceIndex) -> None:
        """Verify every source DB was produced by ``self._scraper``'s class."""
        if self._scraper is None:
            return
        expected = (
            f"{self._scraper.__class__.__module__}:"
            f"{self._scraper.__class__.__name__}"
        )
        mismatches = [
            (path, found)
            for path, found in index.all_scraper_names()
            if found != expected
        ]
        if mismatches:
            index.close()
            self._index = None
            raise ReplayScraperMismatchError(
                expected=expected, mismatches=mismatches
            )

    def seed_anchor_rows(self) -> Iterator[dict]:
        """Entry-request kwargs for each mode-3 anchor (empty for modes 1/2).

        The run inserts these as pending entry requests; their dedup_keys were
        excluded from the index, so they miss and get stubbed for re-fetch.
        """
        yield from self._anchor_rows

    def is_retry_eligible_parent(self, request_id: int | None) -> bool:
        """Whether ``request_id`` resolved against a retry_eligible source row."""
        return request_id is not None and request_id in (
            self._retry_eligible_parents
        )

    async def aclose(self) -> None:
        if self._index is not None:
            self._index.close()
            self._index = None
        self._retry_eligible_parents.clear()

    async def acquire(self, worker_id: int) -> _NoopHandle:
        """Get-or-create the worker's (stateless) handle, stable until release."""
        handle = self._handles.get(worker_id)
        if handle is None:
            handle = _NoopHandle()
            self._handles[worker_id] = handle
        return handle

    async def release(self, worker_id: int) -> None:
        """Drop the worker's handle; the next acquire makes a fresh one."""
        handle = self._handles.pop(worker_id, None)
        if handle is not None:
            await handle.close()

    async def resolve(
        self,
        handle: _NoopHandle,
        queued: QueuedRequest,
        await_conditions: Sequence[AwaitCondition] = (),
    ) -> Response:
        """Return the stored response for ``queued.request`` (await ignored)."""
        request = queued.request
        index = self._require_index()
        entry = index.lookup(_dedup_key_of(request))
        if entry is None:
            entry = index.lookup(
                fallback_replay_key_for_request(request.request)
            )
        if entry is None:
            raise ReplayMiss(
                dedup_key=_dedup_key_of(request), url=request.request.url
            )
        # Children of a retry-eligible match get force-missed by ReplayWorker
        # (unless trust_subtree_after_retry); record the parent here at resolve.
        if entry.retry_eligible:
            self._retry_eligible_parents.add(queued.request_id)
        fetched = await asyncio.to_thread(index.fetch_response, entry)
        return Response(
            status_code=fetched.status_code,
            headers=fetched.headers,
            content=fetched.content,
            text=_decode(fetched.content, fetched.headers),
            url=fetched.url,
            request=request,
        )

    async def resolve_archive(
        self,
        handle: _NoopHandle,
        queued: QueuedRequest,
        decision: object | None = None,
    ) -> ArchiveStream:
        """Stream the stored archive file for ``queued.request``."""
        request = queued.request
        index = self._require_index()
        entry = index.lookup(_dedup_key_of(request))
        if entry is None:
            entry = index.lookup(
                fallback_replay_key_for_request(request.request)
            )
        fetched = (
            await asyncio.to_thread(index.fetch_archive, entry)
            if entry is not None
            else None
        )
        if fetched is None:
            raise ReplayMiss(
                dedup_key=_dedup_key_of(request), url=request.request.url
            )
        return _FileArchiveStream(
            status_code=fetched.status_code,
            headers=fetched.headers,
            url=fetched.url,
            file_path=fetched.file_path,
        )

    async def finish_archiving(self, stream: ArchiveStream) -> None:
        """No-op — the stored archive file belongs to the source DB."""
        return None

    def _require_index(self) -> SourceIndex:
        if self._index is None:
            raise RuntimeError("ReplayTransport used before open()")
        return self._index
