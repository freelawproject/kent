"""Tests for JSRequestPrep / HTTPRequestPrep yield-time preprocessing."""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from kent.common.decorators import step
from kent.common.exceptions import (
    HTMLStructuralAssumptionException,
    ScraperConfigError,
    TransientException,
)
from kent.data_types import (
    BaseRequest,
    BaseScraper,
    DriverRequirement,
    HttpMethod,
    HTTPRequestParams,
    HTTPRequestPrep,
    JSRequestPrep,
    ParsedData,
    Request,
    Response,
)
from kent.driver.persistent_driver.persistent_driver import PersistentDriver
from kent.driver.playwright_driver import PlaywrightDriver
from kent.preps import HCaptchaSolver, ImageCaptchaSolver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _api_results(driver: Any) -> list[dict[str, Any]]:
    """Read all stored result rows as decoded JSON dicts."""
    import json

    async with driver.db._session_factory() as session:
        rows = await session.execute(
            sa.text("SELECT data_json FROM results ORDER BY id")
        )
        return [json.loads(r[0]) for r in rows.all()]


# ---------------------------------------------------------------------------
# 1. JSRequestPrep with a scraper-method prep
# ---------------------------------------------------------------------------


class TestJSRequestPrepScraperMethod:
    @pytest.mark.asyncio
    async def test_js_request_prep_scraper_method(
        self, server_url: str
    ) -> None:
        class SwizzleScraper(BaseScraper[dict[str, Any]]):
            driver_requirements = [DriverRequirement.JS_EVAL]

            def __init__(self, base: str) -> None:
                super().__init__()
                self.base = base

            def get_entry(self) -> Generator[Request, None, None]:
                yield Request(
                    request=HTTPRequestParams(
                        method=HttpMethod.GET,
                        url=f"{self.base}/swizzle/page",
                    ),
                    continuation="parse_page",
                )

            @step
            def parse_page(self, page: Any) -> Generator[Any, None, None]:
                yield JSRequestPrep(
                    Request(
                        request=HTTPRequestParams(
                            method=HttpMethod.GET,
                            url=f"{self.base}/swizzle/api",
                            headers={"accept": "application/json"},
                        ),
                        continuation="parse_api",
                        nonnavigating=True,
                    ),
                    prep_method="add_swizzle_header",
                )

            async def add_swizzle_header(
                self, response: Response, request: BaseRequest, page: Any
            ) -> BaseRequest:
                token = await page.evaluate("() => window.getSwizzleToken()")
                new_headers = {
                    **(request.request.headers or {}),
                    "X-Swizzled": token,
                }
                new_http = replace(request.request, headers=new_headers)
                return replace(request, request=new_http)

            def parse_api(
                self, response: Response
            ) -> Generator[Any, None, None]:
                yield ParsedData({"body": response.text})

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            async with PlaywrightDriver.open(
                SwizzleScraper(server_url),
                db_path,
                headless=True,
                enable_monitor=False,
            ) as driver:
                await driver.run(setup_signal_handlers=False)

                results = await _api_results(driver)
                assert any('"swizzled": true' in r["body"] for r in results)


# ---------------------------------------------------------------------------
# 2. JSRequestPrep with a driver-provided handler
# ---------------------------------------------------------------------------


class _StubHCaptchaSolver(HCaptchaSolver):
    async def prep(
        self,
        response: Response,
        request: BaseRequest,
        page: Any,
    ) -> BaseRequest:
        token = await page.evaluate("() => window.getSwizzleToken()")
        new_headers = {
            **(request.request.headers or {}),
            "X-Swizzled": token,
        }
        new_http = replace(request.request, headers=new_headers)
        return replace(request, request=new_http)


class TestJSRequestPrepProvided:
    @pytest.mark.asyncio
    async def test_js_request_prep_provided_handler(
        self, server_url: str
    ) -> None:
        class ProvidedScraper(BaseScraper[dict[str, Any]]):
            driver_requirements = [
                DriverRequirement.JS_EVAL,
                DriverRequirement.HCAPTCHA_SOLVER,
            ]

            def __init__(self, base: str) -> None:
                super().__init__()
                self.base = base

            def get_entry(self) -> Generator[Request, None, None]:
                yield Request(
                    request=HTTPRequestParams(
                        method=HttpMethod.GET,
                        url=f"{self.base}/swizzle/page",
                    ),
                    continuation="parse_page",
                )

            @step
            def parse_page(self, page: Any) -> Generator[Any, None, None]:
                yield JSRequestPrep(
                    Request(
                        request=HTTPRequestParams(
                            method=HttpMethod.GET,
                            url=f"{self.base}/swizzle/api",
                        ),
                        continuation="parse_api",
                        nonnavigating=True,
                    ),
                    prep_method="provided.hcaptcha_solver",
                )

            def parse_api(
                self, response: Response
            ) -> Generator[Any, None, None]:
                yield ParsedData({"body": response.text})

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            async with PlaywrightDriver.open(
                ProvidedScraper(server_url),
                db_path,
                headless=True,
                enable_monitor=False,
                request_preps=[_StubHCaptchaSolver()],
            ) as driver:
                await driver.run(setup_signal_handlers=False)
                results = await _api_results(driver)
                assert any('"swizzled": true' in r["body"] for r in results)


# ---------------------------------------------------------------------------
# 3. Kwargs forwarded to prep
# ---------------------------------------------------------------------------


class TestKwargsPassedToPrep:
    @pytest.mark.asyncio
    async def test_kwargs_passed_to_prep(self, server_url: str) -> None:
        captured: dict[str, Any] = {}

        class KwargsScraper(BaseScraper[dict[str, Any]]):
            driver_requirements = [DriverRequirement.JS_EVAL]

            def __init__(self, base: str) -> None:
                super().__init__()
                self.base = base

            def get_entry(self) -> Generator[Request, None, None]:
                yield Request(
                    request=HTTPRequestParams(
                        method=HttpMethod.GET,
                        url=f"{self.base}/swizzle/page",
                    ),
                    continuation="parse_page",
                )

            @step
            def parse_page(self, page: Any) -> Generator[Any, None, None]:
                yield HTTPRequestPrep(
                    Request(
                        request=HTTPRequestParams(
                            method=HttpMethod.GET,
                            url=f"{self.base}/swizzle/api",
                            headers={"X-Swizzled": "kent-test-token"},
                        ),
                        continuation="parse_api",
                        nonnavigating=True,
                    ),
                    prep_method="echo_kwargs",
                    foo="bar",
                    baz=42,
                )

            async def echo_kwargs(
                self,
                response: Response,
                request: BaseRequest,
                **kwargs: Any,
            ) -> BaseRequest:
                captured.update(kwargs)
                return request

            def parse_api(
                self, response: Response
            ) -> Generator[Any, None, None]:
                yield None

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            async with PlaywrightDriver.open(
                KwargsScraper(server_url),
                db_path,
                headless=True,
                enable_monitor=False,
            ) as driver:
                await driver.run(setup_signal_handlers=False)

        assert captured == {"foo": "bar", "baz": 42}


# ---------------------------------------------------------------------------
# 4. Missing provider fails open()
# ---------------------------------------------------------------------------


class TestMissingProviderFailsOpen:
    @pytest.mark.asyncio
    async def test_missing_provider_fails_open(self, server_url: str) -> None:
        class NeedsSolverScraper(BaseScraper[dict[str, Any]]):
            driver_requirements = [
                DriverRequirement.JS_EVAL,
                DriverRequirement.IMAGE_CAPTCHA_SOLVER,
            ]

            def get_entry(self) -> Generator[Request, None, None]:
                yield Request(
                    request=HTTPRequestParams(
                        method=HttpMethod.GET,
                        url=f"{server_url}/swizzle/page",
                    ),
                    continuation="parse_page",
                )

            @step
            def parse_page(self, page: Any) -> Generator[Any, None, None]:
                yield None

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            with pytest.raises(ValueError, match="image_captcha_solver"):
                async with PlaywrightDriver.open(
                    NeedsSolverScraper(),
                    db_path,
                    headless=True,
                    enable_monitor=False,
                ):
                    pass


# ---------------------------------------------------------------------------
# 5. Duplicate provider_name fails open()
# ---------------------------------------------------------------------------


class TestDuplicateProviderFailsOpen:
    @pytest.mark.asyncio
    async def test_duplicate_provider_name_fails_open(
        self, server_url: str
    ) -> None:
        class HCaptchaA(HCaptchaSolver):
            async def prep(
                self, response: Response, request: BaseRequest, page: Any
            ) -> BaseRequest:
                return request

        class HCaptchaB(HCaptchaSolver):
            async def prep(
                self, response: Response, request: BaseRequest, page: Any
            ) -> BaseRequest:
                return request

        class TrivialScraper(BaseScraper[dict[str, Any]]):
            driver_requirements = [DriverRequirement.JS_EVAL]

            def get_entry(self) -> Generator[Request, None, None]:
                yield Request(
                    request=HTTPRequestParams(
                        method=HttpMethod.GET,
                        url=f"{server_url}/swizzle/page",
                    ),
                    continuation="parse_page",
                )

            @step
            def parse_page(self, page: Any) -> Generator[Any, None, None]:
                yield None

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            with pytest.raises(ValueError, match="duplicate provider_name"):
                async with PlaywrightDriver.open(
                    TrivialScraper(),
                    db_path,
                    headless=True,
                    enable_monitor=False,
                    request_preps=[HCaptchaA(), HCaptchaB()],
                ):
                    pass


# ---------------------------------------------------------------------------
# 6. Prep transient retries then parent transient
# ---------------------------------------------------------------------------


class TestPrepTransientRetries:
    @pytest.mark.asyncio
    async def test_prep_transient_retries_then_parent_transient(
        self, server_url: str
    ) -> None:
        invocations: list[int] = []

        class FlakyScraper(BaseScraper[dict[str, Any]]):
            driver_requirements = [DriverRequirement.JS_EVAL]

            def __init__(self, base: str) -> None:
                super().__init__()
                self.base = base

            def get_entry(self) -> Generator[Request, None, None]:
                yield Request(
                    request=HTTPRequestParams(
                        method=HttpMethod.GET,
                        url=f"{self.base}/swizzle/page",
                    ),
                    continuation="parse_page",
                )

            @step
            def parse_page(self, page: Any) -> Generator[Any, None, None]:
                yield JSRequestPrep(
                    Request(
                        request=HTTPRequestParams(
                            method=HttpMethod.GET,
                            url=f"{self.base}/swizzle/api",
                        ),
                        continuation="parse_api",
                        nonnavigating=True,
                    ),
                    prep_method="always_transient",
                )

            async def always_transient(
                self, response: Response, request: BaseRequest, page: Any
            ) -> BaseRequest:
                invocations.append(1)
                raise TransientException("flaky prep")

            def parse_api(
                self, response: Response
            ) -> Generator[Any, None, None]:
                yield None

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            async with PlaywrightDriver.open(
                FlakyScraper(server_url),
                db_path,
                headless=True,
                enable_monitor=False,
                # Speed up the parent retry path — let the parent fail rather
                # than wait through the default exponential backoff.
                max_backoff_time=0.1,
                # Keep prep retries fast for the test.
                num_workers=1,
            ) as driver:
                # Tighten prep schedule for speed
                driver.prep_backoff_schedule = (0.01, 0.01, 0.01)
                await driver.run(setup_signal_handlers=False)

        # Each parent attempt should call the prep exactly 4 times
        # (1 initial + 3 retries, per ``prep_backoff_schedule``).
        # With max_backoff_time=0.1 the parent retries a few times; the
        # invariant we check is that the count is a non-zero multiple of 4.
        assert len(invocations) >= 4
        assert len(invocations) % 4 == 0


# ---------------------------------------------------------------------------
# 7. Prep persistent fails parent immediately
# ---------------------------------------------------------------------------


class TestPrepPersistentFailsImmediately:
    @pytest.mark.asyncio
    async def test_prep_persistent_fails_parent_immediately(
        self, server_url: str
    ) -> None:
        invocations: list[int] = []

        class StructScraper(BaseScraper[dict[str, Any]]):
            driver_requirements = [DriverRequirement.JS_EVAL]

            def __init__(self, base: str) -> None:
                super().__init__()
                self.base = base

            def get_entry(self) -> Generator[Request, None, None]:
                yield Request(
                    request=HTTPRequestParams(
                        method=HttpMethod.GET,
                        url=f"{self.base}/swizzle/page",
                    ),
                    continuation="parse_page",
                )

            @step
            def parse_page(self, page: Any) -> Generator[Any, None, None]:
                yield JSRequestPrep(
                    Request(
                        request=HTTPRequestParams(
                            method=HttpMethod.GET,
                            url=f"{self.base}/swizzle/api",
                        ),
                        continuation="parse_api",
                        nonnavigating=True,
                    ),
                    prep_method="always_structural",
                )

            async def always_structural(
                self, response: Response, request: BaseRequest, page: Any
            ) -> BaseRequest:
                invocations.append(1)
                raise HTMLStructuralAssumptionException(
                    selector="//missing",
                    selector_type="xpath",
                    description="missing",
                    expected_min=1,
                    expected_max=1,
                    actual_count=0,
                    request_url="",
                )

            def parse_api(
                self, response: Response
            ) -> Generator[Any, None, None]:
                yield None

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            async with PlaywrightDriver.open(
                StructScraper(server_url),
                db_path,
                headless=True,
                enable_monitor=False,
            ) as driver:
                await driver.run(setup_signal_handlers=False)

        # No retries on persistent error.
        assert len(invocations) == 1


# ---------------------------------------------------------------------------
# 10. Image-captcha round trip
# ---------------------------------------------------------------------------


class _StubImageSolver(ImageCaptchaSolver):
    def __init__(self, solver_url: str) -> None:
        self.solver_url = solver_url

    async def prep(
        self,
        response: Response,
        request: BaseRequest,
        **kwargs: Any,
    ) -> BaseRequest:
        image_url: str = kwargs["image_url"]
        result_field: str = kwargs["result_field"]
        async with httpx.AsyncClient() as c:
            img = (await c.get(image_url)).content
            answer = (await c.post(self.solver_url, content=img)).json()[
                "answer"
            ]
        existing = request.request.data
        existing_dict: dict[str, Any] = (
            dict(existing) if isinstance(existing, dict) else {}
        )
        new_data = {**existing_dict, result_field: answer}
        new_http = replace(request.request, data=new_data)
        return replace(request, request=new_http)


class TestHTTPRequestPrepImageCaptcha:
    @pytest.mark.asyncio
    async def test_http_request_prep_image_captcha_round_trip(
        self, server_url: str
    ) -> None:
        # HTTPRequestPrep is httpx-only by definition (no Page needed), so
        # we test it under the persistent (httpx) driver which can issue
        # real POSTs. Playwright's page.goto can't carry POST bodies.
        class CaptchaScraper(BaseScraper[dict[str, Any]]):
            driver_requirements = [DriverRequirement.IMAGE_CAPTCHA_SOLVER]

            def __init__(self, base: str) -> None:
                super().__init__()
                self.base = base

            def get_entry(self) -> Generator[Request, None, None]:
                yield Request(
                    request=HTTPRequestParams(
                        method=HttpMethod.GET,
                        url=f"{self.base}/captcha/page",
                    ),
                    continuation="parse_page",
                )

            def parse_page(
                self, response: Response
            ) -> Generator[Any, None, None]:
                yield HTTPRequestPrep(
                    Request(
                        request=HTTPRequestParams(
                            method=HttpMethod.POST,
                            url=f"{self.base}/captcha/submit",
                            data={
                                "docket": "C-1",
                                "captcha_token": "abc123",
                            },
                        ),
                        continuation="parse_submit",
                        nonnavigating=True,
                    ),
                    prep_method="provided.image_captcha_solver",
                    image_url=f"{self.base}/captcha/image/abc123",
                    result_field="captcha_answer",
                )

            def parse_submit(
                self, response: Response
            ) -> Generator[Any, None, None]:
                yield ParsedData({"body": response.text})

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            async with PersistentDriver.open(
                CaptchaScraper(server_url),
                db_path,
                enable_monitor=False,
                request_preps=[_StubImageSolver(f"{server_url}/fake-solver")],
            ) as driver:
                await driver.run(setup_signal_handlers=False)
                results = await _api_results(driver)
                assert any('"ok": true' in r["body"] for r in results)


# ---------------------------------------------------------------------------
# 11. Step staging rolls back on prep failure
# ---------------------------------------------------------------------------


class TestStagingRollback:
    @pytest.mark.asyncio
    async def test_step_staging_rolls_back_on_prep_failure(
        self, server_url: str
    ) -> None:
        class RollbackScraper(BaseScraper[dict[str, Any]]):
            driver_requirements = [DriverRequirement.JS_EVAL]

            def __init__(self, base: str) -> None:
                super().__init__()
                self.base = base

            def get_entry(self) -> Generator[Request, None, None]:
                yield Request(
                    request=HTTPRequestParams(
                        method=HttpMethod.GET,
                        url=f"{self.base}/swizzle/page",
                    ),
                    continuation="parse_page",
                )

            @step
            def parse_page(self, page: Any) -> Generator[Any, None, None]:
                yield ParsedData({"before_prep": True})
                yield JSRequestPrep(
                    Request(
                        request=HTTPRequestParams(
                            method=HttpMethod.GET,
                            url=f"{self.base}/swizzle/api",
                        ),
                        continuation="parse_api",
                        nonnavigating=True,
                    ),
                    prep_method="boom",
                )

            async def boom(
                self, response: Response, request: BaseRequest, page: Any
            ) -> BaseRequest:
                raise HTMLStructuralAssumptionException(
                    selector="//x",
                    selector_type="xpath",
                    description="boom",
                    expected_min=1,
                    expected_max=1,
                    actual_count=0,
                    request_url="",
                )

            def parse_api(
                self, response: Response
            ) -> Generator[Any, None, None]:
                yield None

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            async with PlaywrightDriver.open(
                RollbackScraper(server_url),
                db_path,
                headless=True,
                enable_monitor=False,
            ) as driver:
                await driver.run(setup_signal_handlers=False)
                results = await _api_results(driver)
                # Prior ParsedData rolled back together with the prep failure.
                assert {"before_prep": True} not in results


# ---------------------------------------------------------------------------
# 12. JS prep on httpx driver rejected
# ---------------------------------------------------------------------------


class TestJSPrepOnHttpxDriver:
    @pytest.mark.asyncio
    async def test_provided_js_solver_rejected_at_open(
        self, server_url: str
    ) -> None:
        class TrivialScraper(BaseScraper[dict[str, Any]]):
            def get_entry(self) -> Generator[Request, None, None]:
                yield Request(
                    request=HTTPRequestParams(
                        method=HttpMethod.GET,
                        url=f"{server_url}/swizzle/page",
                    ),
                    continuation="parse_page",
                )

            def parse_page(
                self, response: Response
            ) -> Generator[Any, None, None]:
                yield None

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            with pytest.raises(ValueError, match="requires a live"):
                async with PersistentDriver.open(
                    TrivialScraper(),
                    db_path,
                    enable_monitor=False,
                    request_preps=[_StubHCaptchaSolver()],
                ):
                    pass

    @pytest.mark.asyncio
    async def test_yielded_js_prep_on_httpx_runtime_error(
        self, server_url: str
    ) -> None:
        class JSOnHttpxScraper(BaseScraper[dict[str, Any]]):
            def __init__(self, base: str) -> None:
                super().__init__()
                self.base = base

            def get_entry(self) -> Generator[Request, None, None]:
                yield Request(
                    request=HTTPRequestParams(
                        method=HttpMethod.GET,
                        url=f"{self.base}/swizzle/page",
                    ),
                    continuation="parse_page",
                )

            def parse_page(
                self, response: Response
            ) -> Generator[Any, None, None]:
                yield JSRequestPrep(
                    Request(
                        request=HTTPRequestParams(
                            method=HttpMethod.GET,
                            url=f"{self.base}/swizzle/api",
                        ),
                        continuation="parse_api",
                        nonnavigating=True,
                    ),
                    prep_method="never_called",
                )

            async def never_called(
                self, response: Response, request: BaseRequest, page: Any
            ) -> BaseRequest:
                return request

            def parse_api(
                self, response: Response
            ) -> Generator[Any, None, None]:
                yield None

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            async with PersistentDriver.open(
                JSOnHttpxScraper(server_url),
                db_path,
                enable_monitor=False,
                max_backoff_time=0.1,
            ) as driver:
                await driver.run(setup_signal_handlers=False)

                # Parent should have failed (ScraperConfigError surfaced
                # through the worker as the parent's last_error).
                async with driver.db._session_factory() as session:
                    rows = await session.execute(
                        sa.text(
                            "SELECT status, last_error FROM requests "
                            "WHERE url LIKE '%/swizzle/page'"
                        )
                    )
                    status, last_error = rows.first()
                    assert status == "failed"
                    assert "live page" in (last_error or "").lower()


# ---------------------------------------------------------------------------
# 8 + 9. Entry / speculative-step prep yields rejected (no playwright needed)
# ---------------------------------------------------------------------------


class TestEntryStepRejectsPrep:
    @pytest.mark.asyncio
    async def test_entry_step_with_prep_yield_rejected(
        self, server_url: str
    ) -> None:
        class EntryPrepScraper(BaseScraper[dict[str, Any]]):
            def get_entry(self) -> Generator[Any, None, None]:
                yield JSRequestPrep(
                    Request(
                        request=HTTPRequestParams(
                            method=HttpMethod.GET,
                            url=f"{server_url}/swizzle/api",
                        ),
                        continuation="parse_api",
                        nonnavigating=True,
                    ),
                    prep_method="never_called",
                )

            def parse_api(
                self, response: Response
            ) -> Generator[Any, None, None]:
                yield None

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            with pytest.raises(ScraperConfigError, match="entry step"):
                async with PersistentDriver.open(
                    EntryPrepScraper(),
                    db_path,
                    enable_monitor=False,
                ) as driver:
                    await driver.run(setup_signal_handlers=False)
