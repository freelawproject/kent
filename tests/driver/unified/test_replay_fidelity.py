"""Fidelity rig for ``ReplayTransport`` (the v1 replay backend).

Hypothesis generates a run database (request rows + stored responses), then we
point a ``ReplayTransport`` at that file DB and assert it returns the stored
response for each request. The generated DB is the oracle.

Replay matches a request by its **deduplication key**, which is
``hash(method + url + sorted params + data/json)`` (see
``data_types._generate_deduplication_key``) — nothing else. So two properties:

1. **Fidelity** — for every stored row, resolving its request returns exactly
   that response (status code, raw content bytes, response headers, final URL).
2. **Invisibility** — fields that are *not* part of the dedup key do not change
   what replay returns. Holding method + url + params + body fixed while
   varying these yields the same stored response:
       - request headers
       - cookies
       - continuation
       - timeout
   (Also invisible but not exercised here: priority, accumulated_data,
   permanent, bypass_rate_limit, request_id / parent_request_id.)

VISIBLE (they form the dedup key): HTTP method, url, query params, and body
(data/json).

Replay does **not** classify status codes (unlike ``HttpxTransport``): it
returns the stored status verbatim, so a stored 500 comes back as a
``Response(500)`` and is never raised — the fidelity property below covers
error statuses to pin that down.

Beyond fidelity/invisibility, this module pins four more ReplayTransport
properties:

3. **Idempotency / replay-of-replay** — resolving the same stored request
   repeatedly is stable, and a resolved response re-materialized into a fresh DB
   and replayed again is identical.
4. **Concurrent fetch** — ``asyncio.gather`` of distinct ``resolve`` calls on one
   transport (and a two-source-DB variant) shows no cross-corruption; each call
   returns its own stored content/status/url. (``ReplayTransport`` runs
   ``index.fetch_response`` via ``asyncio.to_thread``, so this pins thread-safety.)
5. **Null dedup-key fallback** — a stored row with ``deduplication_key = NULL`` is
   matched via the URL+body fallback and still resolves to its response.
6. **Archive path verbatim** — ``resolve_archive`` streams the SOURCE file path
   (no copy); the streamed bytes equal the on-disk file and ``finish_archiving``
   leaves it in place (replay's file belongs to the source DB).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from jkent.data_types import HttpMethod, HTTPRequestParams, Request, Response
from jkent.driver.unified_driver import (
    QueuedRequest,
    ReplayMiss,
    ReplayTransport,
)
from tests.driver.unified.conftest import (
    ArchiveSpec,
    build_archive_source_db,
    build_source_db,
)

# --- strategies ----------------------------------------------------------

_text = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=8
)
_str_map = st.dictionaries(_text, _text, max_size=3)
_methods = st.sampled_from(
    [
        HttpMethod.GET,
        HttpMethod.POST,
        HttpMethod.PUT,
        HttpMethod.HEAD,
        HttpMethod.DELETE,
    ]
)


@st.composite
def _ingredients(draw: st.DrawFn) -> dict[str, Any]:
    """Everything for one stored row except its (uniquely assigned) URL."""
    return {
        "method": draw(_methods),
        "params": draw(st.none() | _str_map),
        "json": draw(
            st.none()
            | st.dictionaries(_text, st.integers() | _text, max_size=3)
        ),
        "req_headers": draw(st.none() | _str_map),
        # Error codes included on purpose: replay returns the stored status
        # verbatim and never classifies (contrast HttpxTransport), so a stored
        # 500 comes back as Response(500), not a raised exception.
        "status": draw(st.sampled_from([200, 201, 202, 404, 500, 503])),
        "content": draw(st.binary(max_size=300)),
        "resp_headers": draw(_str_map),
    }


@st.composite
def _invisible(draw: st.DrawFn) -> dict[str, Any]:
    """Variation across fields that must NOT change replay's answer."""
    return {
        "req_headers": draw(st.none() | _str_map),
        "cookies": draw(st.none() | _str_map),
        "continuation": draw(st.sampled_from(["parse", "detail", "other"])),
        "timeout": draw(
            st.none() | st.floats(min_value=0.1, max_value=60, allow_nan=False)
        ),
    }


# --- materialization -----------------------------------------------------


@dataclass
class _Entry:
    request: Request
    status: int
    content: bytes
    resp_headers: dict[str, str]
    response_url: str


def _assemble(ing: dict[str, Any], index: int) -> _Entry:
    """Build an entry with a URL unique to ``index`` (→ unique dedup key)."""
    url = f"https://replay.test/r{index}"
    request = Request(
        request=HTTPRequestParams(
            method=ing["method"],
            url=url,
            params=ing["params"],
            json=ing["json"],
            headers=ing["req_headers"],
        ),
        continuation="parse",
    )
    return _Entry(
        request=request,
        status=ing["status"],
        content=ing["content"],
        resp_headers=ing["resp_headers"],
        response_url=f"{url}/final",
    )


def _response_of(entry: _Entry) -> Response:
    """The stored response for an entry (``text`` is not persisted)."""
    return Response(
        status_code=entry.status,
        headers=entry.resp_headers,
        content=entry.content,
        text="",
        url=entry.response_url,
        request=entry.request,
    )


def _rows(entries: list[_Entry]) -> list[tuple[Request, Response]]:
    return [(e.request, _response_of(e)) for e in entries]


# --- properties ----------------------------------------------------------


@pytest.mark.generative
@settings(deadline=None)
@given(ingredients=st.lists(_ingredients(), min_size=1, max_size=6))
def test_replay_returns_each_stored_response(
    schema_template: Path,
    tmp_path_factory: pytest.TempPathFactory,
    ingredients: list[dict[str, Any]],
) -> None:
    entries = [_assemble(ing, i) for i, ing in enumerate(ingredients)]
    dest = tmp_path_factory.mktemp("run") / "run.db"

    async def run() -> None:
        await build_source_db(schema_template, dest, _rows(entries))
        transport = ReplayTransport([dest])
        await transport.open()
        handle = await transport.acquire(0)
        try:
            for e in entries:
                resp = await transport.resolve(
                    handle, QueuedRequest(request=e.request, request_id=1)
                )
                assert resp.status_code == e.status
                assert resp.content == e.content
                assert resp.headers == e.resp_headers
                assert resp.url == e.response_url
                assert resp.request is e.request
        finally:
            await transport.aclose()

    asyncio.run(run())


@pytest.mark.generative
@settings(deadline=None)
@given(
    ingredients=_ingredients(),
    variations=st.lists(_invisible(), min_size=1, max_size=5),
)
def test_invisible_fields_do_not_change_resolution(
    schema_template: Path,
    tmp_path_factory: pytest.TempPathFactory,
    ingredients: dict[str, Any],
    variations: list[dict[str, Any]],
) -> None:
    entry = _assemble(ingredients, 0)
    dest = tmp_path_factory.mktemp("run") / "run.db"
    base = entry.request.request  # method + url + params + json define the key

    async def run() -> None:
        await build_source_db(schema_template, dest, _rows([entry]))
        transport = ReplayTransport([dest])
        await transport.open()
        handle = await transport.acquire(0)
        try:
            for v in variations:
                variant = Request(
                    request=HTTPRequestParams(
                        method=base.method,
                        url=base.url,
                        params=base.params,
                        json=base.json,
                        headers=v["req_headers"],
                        cookies=v["cookies"],
                        timeout=v["timeout"],
                    ),
                    continuation=v["continuation"],
                )
                # Same method+url+params+body → same dedup key as the
                # stored row.
                assert (
                    variant.deduplication_key
                    == entry.request.deduplication_key
                )
                resp = await transport.resolve(
                    handle, QueuedRequest(request=variant, request_id=1)
                )
                assert resp.content == entry.content
                assert resp.status_code == entry.status
                assert resp.url == entry.response_url
        finally:
            await transport.aclose()

    asyncio.run(run())


async def test_unstored_request_raises_miss(
    schema_template: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    dest = tmp_path_factory.mktemp("run") / "empty.db"
    await build_source_db(schema_template, dest, [])

    transport = ReplayTransport([dest])
    await transport.open()
    handle = await transport.acquire(0)
    try:
        req = Request(
            request=HTTPRequestParams(
                method=HttpMethod.GET, url="https://replay.test/missing"
            ),
            continuation="parse",
        )
        with pytest.raises(ReplayMiss):
            await transport.resolve(
                handle, QueuedRequest(request=req, request_id=1)
            )
    finally:
        await transport.aclose()


# --- idempotency / replay-of-replay --------------------------------------


@pytest.mark.generative
@settings(deadline=None)
@given(ingredients=st.lists(_ingredients(), min_size=1, max_size=4))
def test_resolve_is_idempotent_and_replay_of_replay_stable(
    schema_template: Path,
    tmp_path_factory: pytest.TempPathFactory,
    ingredients: list[dict[str, Any]],
) -> None:
    """Same request resolves identically N times; re-stored + replayed is stable."""
    entries = [_assemble(ing, i) for i, ing in enumerate(ingredients)]
    work = tmp_path_factory.mktemp("idem")
    dest = work / "run.db"

    async def run() -> None:
        await build_source_db(schema_template, dest, _rows(entries))
        transport = ReplayTransport([dest])
        await transport.open()
        handle = await transport.acquire(0)
        try:
            # (a) Resolving the same stored request repeatedly is identical.
            for e in entries:
                queued = QueuedRequest(request=e.request, request_id=1)
                first = await transport.resolve(handle, queued)
                for _ in range(3):
                    again = await transport.resolve(handle, queued)
                    assert again.status_code == first.status_code
                    assert again.content == first.content
                    assert again.headers == first.headers
                    assert again.url == first.url
        finally:
            await transport.aclose()

        # (b) Replay-of-replay: re-store each resolved response into a fresh DB,
        # replay it, and assert it matches the original stored row.
        dest2 = work / "run2.db"
        await build_source_db(schema_template, dest2, _rows(entries))
        transport2 = ReplayTransport([dest2])
        await transport2.open()
        handle2 = await transport2.acquire(0)
        try:
            for e in entries:
                resp = await transport2.resolve(
                    handle2, QueuedRequest(request=e.request, request_id=1)
                )
                assert resp.status_code == e.status
                assert resp.content == e.content
                assert resp.headers == e.resp_headers
                assert resp.url == e.response_url
        finally:
            await transport2.aclose()

    asyncio.run(run())


# --- concurrent fetch ----------------------------------------------------


@pytest.mark.generative
@settings(deadline=None)
@given(ingredients=st.lists(_ingredients(), min_size=2, max_size=6))
def test_concurrent_resolve_no_cross_corruption(
    schema_template: Path,
    tmp_path_factory: pytest.TempPathFactory,
    ingredients: list[dict[str, Any]],
) -> None:
    """gather()-ed resolves on one transport each return THEIR stored row."""
    entries = [_assemble(ing, i) for i, ing in enumerate(ingredients)]
    dest = tmp_path_factory.mktemp("concurrent") / "run.db"

    async def run() -> None:
        await build_source_db(schema_template, dest, _rows(entries))
        transport = ReplayTransport([dest])
        await transport.open()
        handle = await transport.acquire(0)
        try:
            results = await asyncio.gather(
                *(
                    transport.resolve(
                        handle, QueuedRequest(request=e.request, request_id=1)
                    )
                    for e in entries
                )
            )
            for e, resp in zip(entries, results, strict=True):
                assert resp.status_code == e.status
                assert resp.content == e.content
                assert resp.headers == e.resp_headers
                assert resp.url == e.response_url
        finally:
            await transport.aclose()

    asyncio.run(run())


@pytest.mark.generative
@settings(deadline=None)
@given(
    left=st.lists(_ingredients(), min_size=1, max_size=3),
    right=st.lists(_ingredients(), min_size=1, max_size=3),
)
def test_concurrent_resolve_across_two_source_dbs(
    schema_template: Path,
    tmp_path_factory: pytest.TempPathFactory,
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
) -> None:
    """Concurrent resolves spanning two source DBs stay un-corrupted."""
    work = tmp_path_factory.mktemp("two_db")
    # Disjoint URL ranges so each entry has a unique dedup key across DBs.
    left_entries = [_assemble(ing, i) for i, ing in enumerate(left)]
    right_entries = [_assemble(ing, 100 + i) for i, ing in enumerate(right)]
    db_a = work / "a.db"
    db_b = work / "b.db"
    entries = left_entries + right_entries

    async def run() -> None:
        await build_source_db(schema_template, db_a, _rows(left_entries))
        await build_source_db(schema_template, db_b, _rows(right_entries))
        transport = ReplayTransport([db_a, db_b])
        await transport.open()
        handle = await transport.acquire(0)
        try:
            results = await asyncio.gather(
                *(
                    transport.resolve(
                        handle, QueuedRequest(request=e.request, request_id=1)
                    )
                    for e in entries
                )
            )
            for e, resp in zip(entries, results, strict=True):
                assert resp.status_code == e.status
                assert resp.content == e.content
                assert resp.headers == e.resp_headers
                assert resp.url == e.response_url
        finally:
            await transport.aclose()

    asyncio.run(run())


# --- null dedup-key fallback ---------------------------------------------


@pytest.mark.generative
@settings(deadline=None)
@given(ingredients=_ingredients())
def test_null_dedup_key_resolved_via_url_body_fallback(
    schema_template: Path,
    tmp_path_factory: pytest.TempPathFactory,
    ingredients: dict[str, Any],
) -> None:
    """A NULL-dedup_key row is matched by the URL+body fallback key."""
    entry = _assemble(ingredients, 0)
    dest = tmp_path_factory.mktemp("null_dedup") / "run.db"

    async def run() -> None:
        # dedup_key=None forces a NULL deduplication_key column; the request
        # still carries its own real key, so the primary lookup misses and the
        # URL+body fallback is what matches.
        await build_source_db(
            schema_template, dest, _rows([entry]), dedup_key=None
        )
        transport = ReplayTransport([dest])
        await transport.open()
        handle = await transport.acquire(0)
        try:
            resp = await transport.resolve(
                handle, QueuedRequest(request=entry.request, request_id=1)
            )
            assert resp.status_code == entry.status
            assert resp.content == entry.content
            assert resp.headers == entry.resp_headers
            assert resp.url == entry.response_url
        finally:
            await transport.aclose()

    asyncio.run(run())


# --- archive path verbatim (no copy) -------------------------------------


@pytest.mark.generative
@settings(deadline=None)
@given(content=st.binary(max_size=400))
def test_archive_streams_source_file_verbatim_without_deleting(
    schema_template: Path,
    tmp_path_factory: pytest.TempPathFactory,
    content: bytes,
) -> None:
    """resolve_archive streams the SOURCE file; finish_archiving leaves it intact."""
    work = tmp_path_factory.mktemp("archive_verbatim")
    dest = work / "run.db"
    file_path = work / "archive.bin"
    request = Request(
        request=HTTPRequestParams(
            method=HttpMethod.GET, url="https://archive.test/file"
        ),
        continuation="parse",
        archive=True,
    )

    async def run() -> None:
        await build_archive_source_db(
            schema_template, dest, [ArchiveSpec(request, file_path, content)]
        )
        transport = ReplayTransport([dest])
        await transport.open()
        handle = await transport.acquire(0)
        try:
            stream = await transport.resolve_archive(
                handle, QueuedRequest(request=request, request_id=1)
            )
            streamed = bytearray()
            async for chunk in stream:
                streamed.extend(chunk)
            assert bytes(streamed) == content
            assert bytes(streamed) == file_path.read_bytes()

            # finish_archiving must NOT delete the source-owned file.
            await transport.finish_archiving(stream)
            assert file_path.exists()
            assert file_path.read_bytes() == content
        finally:
            await transport.aclose()

    asyncio.run(run())
