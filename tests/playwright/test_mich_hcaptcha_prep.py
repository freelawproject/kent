"""Validation test for the Michigan-style hCaptcha JSRequestPrep flow.

Uses the ``/mich-mock/*`` endpoints in [tests/mock_server.py] to stand
in for ``courts.michigan.gov``: an SPA-style case page exposes
``window.hcaptcha.execute({async: true})``, and a captcha-gated JSON API
requires the resulting JWT in a ``captchatoken`` header.

The test exercises:

1. ``MichHCaptchaSolver.prep`` — mirrors the real Michigan integration
   (``hcaptcha.execute`` → JWT in the ``captchatoken`` header).
2. ``PlaywrightDriver``'s per-request header propagation (the prep'd
   header reaches the actual fetch via ``set_extra_http_headers``).

Once this validates end-to-end, ``MichHCaptchaSolver`` is ready to be
hoisted into juriscraper alongside the Michigan scraper.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa

from kent.common.decorators import step
from kent.common.exceptions import TransientException
from kent.data_types import (
    BaseRequest,
    BaseScraper,
    DriverRequirement,
    HttpMethod,
    HTTPRequestParams,
    JSRequestPrep,
    ParsedData,
    Request,
    Response,
)
from kent.driver.playwright_driver import PlaywrightDriver
from kent.preps import HCaptchaSolver

if TYPE_CHECKING:
    from playwright.async_api import Page


# ---------------------------------------------------------------------------
# Solver under validation
# ---------------------------------------------------------------------------


class MichHCaptchaSolver(HCaptchaSolver):
    """Resolves Michigan's invisible-execute hCaptcha into a header.

    The Michigan SPA loads hCaptcha with sitekey
    ``9bf9cc63-9d2e-4f54-98f8-8d3063233b9c`` and obtains a JWT per
    request via ``hcaptcha.execute({async: true})``. This solver
    reproduces that flow from inside a ``JSRequestPrep``.

    Once validated, this class moves to
    ``juriscraper.sd.state.michigan.courts_michigan_gov.hcaptcha``.
    """

    SDK_READY_TIMEOUT_MS = 10_000

    async def prep(
        self,
        response: Response,
        request: BaseRequest,
        page: Page,
    ) -> BaseRequest:
        try:
            await page.wait_for_function(
                "() => window.hcaptcha "
                "&& typeof window.hcaptcha.execute === 'function'",
                timeout=self.SDK_READY_TIMEOUT_MS,
            )
        except Exception as e:
            raise TransientException(
                "hCaptcha SDK not loaded on parent page within "
                f"{self.SDK_READY_TIMEOUT_MS}ms"
            ) from e

        try:
            token = await page.evaluate(
                "async () => {"
                "  const r = await window.hcaptcha.execute("
                "    {async: true}"
                "  );"
                "  return (r && r.response) || null;"
                "}"
            )
        except Exception as e:
            raise TransientException(f"hcaptcha.execute() failed: {e}") from e

        if not token:
            raise TransientException("hcaptcha.execute() returned no token")

        new_headers = {
            **(request.request.headers or {}),
            "captchatoken": token,
        }
        new_http = replace(request.request, headers=new_headers)
        return replace(request, request=new_http)


# ---------------------------------------------------------------------------
# Validation test
# ---------------------------------------------------------------------------


class TestMichHCaptchaPrep:
    @pytest.mark.asyncio
    async def test_solver_drives_captcha_gated_api(
        self, server_url: str
    ) -> None:
        """End-to-end: navigate to the SPA case page, prep extracts the JWT,
        the inner Request fetches the captcha-gated JSON API."""

        class MichMockScraper(BaseScraper[dict[str, Any]]):
            driver_requirements = [
                DriverRequirement.JS_EVAL,
                DriverRequirement.HCAPTCHA_SOLVER,
            ]

            def __init__(self, base: str, case_id: str) -> None:
                super().__init__()
                self.base = base
                self.case_id = case_id

            def get_entry(self) -> Generator[Request, None, None]:
                yield Request(
                    request=HTTPRequestParams(
                        method=HttpMethod.GET,
                        url=f"{self.base}/mich-mock/case/{self.case_id}",
                    ),
                    continuation="parse_case_page",
                )

            @step
            def parse_case_page(self, page: Any) -> Generator[Any, None, None]:
                # The parent page just loaded the SPA; window.hcaptcha is
                # available. Yield a JSRequestPrep that asks the solver
                # for a captchatoken header, then fires the gated API.
                yield JSRequestPrep(
                    Request(
                        request=HTTPRequestParams(
                            method=HttpMethod.GET,
                            url=f"{self.base}/mich-mock/api/case/{self.case_id}",
                        ),
                        continuation="parse_case_detail",
                        nonnavigating=True,
                    ),
                    prep_method="provided.hcaptcha_solver",
                )

            def parse_case_detail(
                self, response: Response
            ) -> Generator[Any, None, None]:
                # response.text is the API's JSON wrapped in a Playwright
                # JSON viewer; substring-test for stability.
                yield ParsedData({"body": response.text})

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            async with PlaywrightDriver.open(
                MichMockScraper(server_url, "380502"),
                db_path,
                headless=True,
                enable_monitor=False,
                request_preps=[MichHCaptchaSolver()],
            ) as driver:
                await driver.run(setup_signal_handlers=False)

                async with driver.db._session_factory() as session:
                    rows = await session.execute(
                        sa.text("SELECT data_json FROM results ORDER BY id")
                    )
                    raw_rows = [r[0] for r in rows.all()]

        import json

        # Decode the outer ParsedData JSON envelope, then inspect the
        # rendered HTML body Playwright produced.
        bodies = [json.loads(r)["body"] for r in raw_rows]

        # Captcha was passed → API returned the OK payload, not the 403.
        assert any('"captchaPassed": true' in b for b in bodies), bodies
        assert any('"caseId": "380502"' in b for b in bodies), bodies
