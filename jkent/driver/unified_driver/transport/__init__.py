"""The ``Transport`` seam: the one thing the unified driver's backends differ on.

The driver core owns orchestration — the queue, the worker pool, storage,
rate limiting, retries. A ``Transport`` owns the other half: turning a
request into a response, the per-worker resource that work runs on, and
recovery when that resource dies. HTTP, Playwright, and replay become
three implementations of this one interface instead of a base class plus
overrides.

Mostly interface (the ``Transport`` ABC and its ``WorkerHandle`` /
``ArchiveStream`` collaborators); the handful of concrete bases every
transport would otherwise duplicate live here too — :class:`NoopHandle`
(HTTP and replay) and :class:`FileArchiveStream` (Playwright and replay).
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Generic,
    TypeAlias,
    TypeVar,
)

from jkent.data_types import (
    WaitForLoadState,
    WaitForSelector,
    WaitForTimeout,
    WaitForURL,
)
from jkent.driver.unified_driver.lifecycle import AsyncLifecycle

if TYPE_CHECKING:
    from jkent.data_types import (
        ArchiveDecision,
        Request,
        Response,
    )

# A single wait directive from a step's ``await_list``. Transports that
# can't honor waits (HTTP, replay) ignore them; Playwright applies them
# before snapshotting the DOM.
AwaitCondition: TypeAlias = (
    WaitForSelector | WaitForLoadState | WaitForURL | WaitForTimeout
)


@dataclass(frozen=True)
class QueuedRequest:
    """A request paired with the persistence ids the queue assigned it.

    Formalizes the tuple the queue hands a worker: the request spec plus
    its row id and its parent's row id. Transports that need to reach the
    database for execution use these — Playwright stages the parent tab
    from ``parent_request_id`` and tags captured incidentals with
    ``request_id``. HTTP and replay ignore the ids.
    """

    request: Request
    request_id: int
    parent_request_id: int | None = None


class WorkerHandle(abc.ABC):
    """A transport's per-worker resource (a browser page, or nothing).

    Acquired once per worker via :meth:`Transport.acquire`, reused across
    requests, and reset between them. HTTP and replay hand back a no-op
    handle (:class:`NoopHandle`) since they hold no per-worker state.
    """

    @abc.abstractmethod
    async def reset_for_reuse(self) -> None:
        """Clear per-request state so the handle is ready for the next request."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release the underlying per-worker resource."""


class NoopHandle(WorkerHandle):
    """The handle for transports that hold no per-worker resource.

    Shared by HTTP (httpx pools internally) and replay (reads from a
    source DB); both hand one of these back from ``acquire`` so the worker
    loop has a uniform handle to reset and close.
    """

    async def reset_for_reuse(self) -> None:
        return None

    async def close(self) -> None:
        return None


class ArchiveStream(abc.ABC):
    """A streamed archive body plus its response metadata.

    Returned by :meth:`Transport.resolve_archive`. The caller iterates the
    body in chunks and writes it to storage, then hands this object back to
    :meth:`Transport.finish_archiving` to release any transport-side
    backing — e.g. the temp file a Playwright download is staged to before
    it can be streamed.

    The metadata trio (``status_code``/``headers``/``url``) is stored by
    this base ``__init__``; subclasses add their own body source and
    implement :meth:`__aiter__`.
    """

    status_code: int
    headers: dict[str, str]
    url: str

    def __init__(
        self, *, status_code: int, headers: dict[str, str], url: str
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self.url = url

    @abc.abstractmethod
    def __aiter__(self) -> AsyncIterator[bytes]:
        """Iterate the response body in chunks."""


class FileArchiveStream(ArchiveStream):
    """An :class:`ArchiveStream` that reads a local file in chunks.

    Shared by the transports that can only surface an archive as a file on
    disk: Playwright stages a download to a temp file (deleted by its
    ``finish_archiving``), and replay points at a stored archive file it
    does not own (its ``finish_archiving`` is the inherited no-op). The
    deletion policy lives in each transport, not here.
    """

    def __init__(
        self,
        *,
        status_code: int,
        headers: dict[str, str],
        url: str,
        file_path: str,
        chunk_size: int = 65536,
    ) -> None:
        super().__init__(status_code=status_code, headers=headers, url=url)
        # Public: replay references the source file verbatim (no copy), so
        # the ReplayWorker reads this path directly instead of streaming a
        # copy.
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


HandleT = TypeVar("HandleT", bound=WorkerHandle)


class Transport(AsyncLifecycle, Generic[HandleT]):
    """Executes requests for the driver, owning its own resource lifecycle.

    Composes :class:`AsyncLifecycle` (``open``/``aclose`` for the run-scoped
    resource, e.g. a browser engine). Crash recovery is **internal**, not a
    caller concern: a dead resource surfaces as a ``TransientException`` from
    ``resolve`` (which poisons the worker's handle), and the next ``acquire``
    rebuilds it — escalating to a single-flight restart of the shared
    resource when a crash poisons the whole handle cache. Callers drive none
    of this; they retry transients and re-``acquire``.
    """

    @abc.abstractmethod
    async def acquire(self, worker_id: int) -> HandleT:
        """Get or create the per-worker handle for ``worker_id``.

        A handle poisoned by a crash is rebuilt here; when the shared
        resource itself is dead this escalates to a single-flight restart of
        it. May raise ``TransientException`` if the rebuild can't complete
        (e.g. a context that can't be restarted), so callers treat ``acquire``
        like ``resolve`` for retry purposes.
        """

    @abc.abstractmethod
    async def release(self, worker_id: int) -> None:
        """Close and forget the handle for ``worker_id`` (worker exiting)."""

    @abc.abstractmethod
    async def resolve(
        self,
        handle: HandleT,
        queued: QueuedRequest,
        await_conditions: Sequence[AwaitCondition] = (),
    ) -> Response:
        """Fetch ``queued.request`` and return its response.

        ``await_conditions`` come from the target step's ``await_list``.
        Transports that can't wait (HTTP, replay) ignore them; Playwright
        applies them before snapshotting. Playwright also stages the parent
        tab from ``queued.parent_request_id`` and persists captured
        incidentals against ``queued.request_id`` via its own DB reference.

        A dead browser surfaces as a ``TransientException`` (the handle is
        poisoned so the next ``acquire`` rebuilds and, if needed, restarts).
        """

    @abc.abstractmethod
    async def resolve_archive(
        self,
        handle: HandleT,
        queued: QueuedRequest,
        decision: ArchiveDecision | None = None,
    ) -> ArchiveStream:
        """Begin an archive download and return a stream of its body.

        All archiving is streamed: the caller reads the body in chunks from
        the returned :class:`ArchiveStream` and writes it to storage. How
        the transport produces those chunks is its own concern — Playwright
        can only obtain a download as a local file, so it stages to a temp
        file and streams from there, released later by ``finish_archiving``.
        ``queued.request`` is an archive request; ``decision`` is a
        pre-computed archive-handler verdict, consulted by the transport
        itself when None.
        """

    async def finish_archiving(self, stream: ArchiveStream) -> None:
        """Release any transport-side backing for ``stream``.

        Called once the caller has fully consumed the stream and persisted
        the body — the place to delete a staged temp file. Defaults to a
        no-op for transports that own nothing to release (replay reads a
        file it does not own); HTTP closes its streaming connection and
        Playwright deletes its staged temp file by overriding this.
        """
        return None
