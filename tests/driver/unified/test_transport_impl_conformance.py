"""Bind the reusable conformance suites to the two real v1 transports.

The reference fake in ``test_transport_conformance`` proves the suite is
self-consistent; this file proves the suite passes against the actual
``HttpxTransport`` and ``ReplayTransport`` — the Phase 0 "wire the real impls
into conformance" step.

Both transports hold no per-worker state, so their ``WorkerHandle`` is a no-op;
the suite's stability/freshness checks pin the get-or-create contract
(``acquire`` is stable per ``worker_id`` until ``release``) that they now honor.

To make ``resolve``/``resolve_archive`` actually resolve under the conformance
methods, each subject is paired with a coordinated backing:

- ``HttpxTransport`` — a live aiohttp server answering ``200`` + a non-empty body
  for any path; ``make_queued`` points at it.
- ``ReplayTransport`` — a one-row run DB whose single request is both a stored
  response *and* an archived file (so the same ``make_queued`` request satisfies
  both ``resolve`` and ``resolve_archive``); ``make_queued`` wraps that request.

The two transports are also run through ``AsyncLifecycleConformance``. Its
resource-leak test is written against the reference fake, so each subclass
overrides it with a transport-specific "backing is released after aclose" check.
"""

from __future__ import annotations

import shutil
import sqlite3
from typing import TYPE_CHECKING

import pytest
from aiohttp import web

from jkent.data_types import (
    HttpMethod,
    HTTPRequestParams,
    Request,
)
from jkent.driver.replay.source_index import serialize_url_and_body
from jkent.driver.unified_driver import (
    HttpxTransport,
    QueuedRequest,
    ReplayTransport,
)
from jkent.driver.unified_driver.compression import compress
from tests.driver.unified.conftest import start_app
from tests.driver.unified.test_async_lifecycle_conformance import (
    AsyncLifecycleConformance,
)
from tests.driver.unified.test_transport_conformance import (
    TransportConformance,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from jkent.driver.unified_driver import AsyncLifecycle


# --- HttpxTransport: live server ------------------------------------------


def _ok_app() -> web.Application:
    """Answer every request with 200 + a non-empty body (response & archive)."""

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=200, body=b"<html>conformance</html>")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


class TestHttpxTransportConformance(TransportConformance):
    """The conformance suite against a real ``HttpxTransport`` + server."""

    @pytest.fixture
    async def subject(self) -> AsyncIterator[HttpxTransport]:
        server = await start_app(_ok_app())
        self._base_url = server.base_url  # type: ignore
        transport = HttpxTransport()
        try:
            yield transport
        finally:
            # aclose is idempotent and safe on a never-opened transport, so
            # this releases the httpx client even if a test fails before its
            # own aclose() runs.
            await transport.aclose()
            await server.runner.cleanup()

    def make_queued(self, *, request_id: int = 1) -> QueuedRequest:
        return QueuedRequest(
            request=Request(
                request=HTTPRequestParams(
                    method=HttpMethod.GET,
                    url=f"{self._base_url}/r",  # type: ignore
                ),
                continuation="parse",
            ),
            request_id=request_id,
        )


# --- ReplayTransport: one-row run DB --------------------------------------

# Distinct sentinels for the two serving paths: the inline (compressed)
# response body vs. the on-disk archive payload. Kept separate so a content
# assertion can prove resolve() and resolve_archive() return their own
# payload, not each other's.
_RESPONSE_BODY = b"replay-response-body"
_ARCHIVE_PAYLOAD = b"replay-archive-payload"


def _materialize_dual(
    template: Path, dest: Path, workdir: Path, request: Request
) -> None:
    """One row that is both a stored response and an archived file.

    ``request_type='archive'`` with ``content_compressed`` set and an
    ``archived_files`` companion lets the *same* dedup key satisfy both
    ``fetch_response`` (decompresses the inline body) and ``fetch_archive``
    (reads the on-disk file) — so one ``make_queued`` request drives both the
    ``resolve`` and ``resolve_archive`` conformance checks.
    """
    shutil.copy(template, dest)
    url, body = serialize_url_and_body(request.request)
    content = _RESPONSE_BODY
    compressed = compress(content)
    archive_file = workdir / "archive.bin"
    archive_file.write_bytes(_ARCHIVE_PAYLOAD)
    assert isinstance(request.deduplication_key, str)
    conn = sqlite3.connect(str(dest))
    try:
        cur = conn.execute(
            """
            INSERT INTO requests (
                status, priority, queue_counter, method, url, body,
                continuation, current_location, deduplication_key,
                request_type, response_status_code, response_url,
                response_headers_json, content_compressed,
                content_size_original, content_size_compressed,
                compression_dict_id, completed_at_ns, created_at_ns)
            VALUES ('completed', 9, 1, ?, ?, ?, 'parse', '', ?, 'archive',
                200, ?, '{}', ?, ?, ?, NULL, 1, 1)
            """,
            (
                request.request.method.value,
                url,
                body,
                request.deduplication_key,
                request.request.url,
                compressed,
                len(content),
                len(compressed),
            ),
        )
        conn.execute(
            "INSERT INTO archived_files (request_id, file_path, original_url) "
            "VALUES (?, ?, ?)",
            (cur.lastrowid, str(archive_file), request.request.url),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


class TestReplayTransportConformance(TransportConformance):
    """The conformance suite against a real ``ReplayTransport`` + run DB."""

    @pytest.fixture
    def subject(
        self,
        schema_template: Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> ReplayTransport:
        workdir = tmp_path_factory.mktemp("replay_conf")
        dest = workdir / "run.db"
        self._request = Request(  # type: ignore
            request=HTTPRequestParams(
                method=HttpMethod.GET, url="https://conf.test/r"
            ),
            continuation="parse",
        )
        _materialize_dual(schema_template, dest, workdir, self._request)
        return ReplayTransport([dest])

    def make_queued(self, *, request_id: int = 1) -> QueuedRequest:
        return QueuedRequest(request=self._request, request_id=request_id)  # type: ignore

    async def test_resolve_returns_stored_response_body(
        self, subject: ReplayTransport
    ) -> None:
        """``resolve`` decompresses the stored inline body, not the archive."""
        await subject.open()
        handle = await subject.acquire(0)
        response = await subject.resolve(handle, self.make_queued())
        assert response.status_code == 200
        assert response.content == _RESPONSE_BODY
        await subject.aclose()

    async def test_resolve_archive_streams_stored_file(
        self, subject: ReplayTransport
    ) -> None:
        """``resolve_archive`` streams the on-disk file, not the response body."""
        await subject.open()
        handle = await subject.acquire(0)
        stream = await subject.resolve_archive(handle, self.make_queued())
        assert stream.status_code == 200
        body = b"".join([chunk async for chunk in stream])
        assert body == _ARCHIVE_PAYLOAD
        await subject.finish_archiving(stream)
        await subject.aclose()


# --- AsyncLifecycle conformance for both transports -----------------------


class TestHttpxTransportLifecycle(AsyncLifecycleConformance):
    """``HttpxTransport`` honors the open -> use -> aclose lifecycle."""

    @pytest.fixture
    def subject(self) -> AsyncLifecycle:
        return HttpxTransport()

    def live_resources(self, subject: AsyncLifecycle) -> int:
        """The httpx client acquired in ``open`` and dropped by ``aclose``."""
        assert isinstance(subject, HttpxTransport)
        return 0 if subject._client is None else 1


class TestReplayTransportLifecycle(AsyncLifecycleConformance):
    """``ReplayTransport`` honors the open -> use -> aclose lifecycle."""

    @pytest.fixture
    def subject(
        self,
        schema_template: Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> AsyncLifecycle:
        dest = tmp_path_factory.mktemp("replay_life") / "empty.db"
        shutil.copy(schema_template, dest)
        return ReplayTransport([dest])

    def live_resources(self, subject: AsyncLifecycle) -> int:
        """The source index built in ``open`` and dropped by ``aclose``."""
        assert isinstance(subject, ReplayTransport)
        return 0 if subject._index is None else 1
