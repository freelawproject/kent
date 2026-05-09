"""Validation test for the WordImageCaptcha HTTPRequestPrep solver.

Mirrors the Utah Court of Appeals login flow at
``apps.utcourts.gov/CourtsPublicWEB/LoginServlet`` against local mock
endpoints in [tests/mock_server.py]:

- ``/utah-mock/login`` — Utah-style login HTML with a captcha image,
  a hidden ``embedded`` token, and a ``captchaEntry`` text input.
- ``/utah-mock/captcha-image/{token}`` — serves "image" bytes whose
  decode is the answer string.
- ``/utah-mock/login-submit`` — accepts the form, validates
  ``captchaEntry`` against the token, returns the search-page HTML.
- ``/utah-mock/resolve`` — stand-in for ``thebes/resolve.py``: accepts
  a multipart POST and returns the recognized text as plain text.

The test exercises a two-step scraper: GET login → parse the form and
yield a ``HTTPRequestPrep`` for the form POST, with
``WordImageCaptcha`` as the prep provider. WordImageCaptcha downloads
the image, posts it to the resolver, and bakes the answer into the
form's ``captchaEntry`` field.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from kent.common.decorators import step
from kent.common.exceptions import HTMLStructuralAssumptionException
from kent.data_types import (
    BaseScraper,
    HttpMethod,
    HTTPRequestParams,
    HTTPRequestPrep,
    ParsedData,
    Request,
    Response,
)
from kent.driver.persistent_driver.persistent_driver import PersistentDriver
from kent.preps import WordImageCaptcha


class TestWordImageCaptcha:
    async def test_utah_style_login_through_resolver(
        self, server_url: str, db_path: Path
    ) -> None:
        """End-to-end: GET login → prep solves captcha → POST → search page."""

        class UtahLikeScraper(BaseScraper[dict[str, Any]]):
            def __init__(self, base: str) -> None:
                super().__init__()
                self.base = base

            def get_entry(self) -> Generator[Request, None, None]:
                yield Request(
                    request=HTTPRequestParams(
                        method=HttpMethod.GET,
                        url=f"{self.base}/utah-mock/login",
                    ),
                    continuation="parse_login_page",
                    nonnavigating=True,
                )

            @step
            def parse_login_page(
                self, lxml_tree: Any, response: Response
            ) -> Generator[Any, None, None]:
                # Extract the captcha image URL and the hidden ``embedded``
                # token from the login form. Both are session-scoped on
                # the real Utah site; the mock just keys them off a fixed
                # token.
                img = lxml_tree.checked_xpath(
                    "//img[@id='captcha-img']", "captcha-img"
                )[0]
                embedded_input = lxml_tree.checked_xpath(
                    "//input[@name='embedded']", "embedded-input"
                )[0]

                img_src = img.get("src") or ""
                if not img_src:
                    raise HTMLStructuralAssumptionException(
                        selector="//img[@id='captcha-img']/@src",
                        selector_type="xpath",
                        description="captcha image has no src",
                        expected_min=1,
                        expected_max=1,
                        actual_count=0,
                        request_url=response.url,
                    )

                # Resolve relative to the login page's URL.
                if img_src.startswith("/"):
                    image_url = f"{self.base}{img_src}"
                else:
                    image_url = img_src

                embedded = embedded_input.get("value") or ""

                yield HTTPRequestPrep(
                    Request(
                        request=HTTPRequestParams(
                            method=HttpMethod.POST,
                            url=f"{self.base}/utah-mock/login-submit",
                            data={
                                "mode": "edit",
                                "embedded": embedded,
                                "task": "DOCKET",
                            },
                        ),
                        continuation="parse_search_page",
                        nonnavigating=True,
                    ),
                    prep_method="provided.image_captcha_solver",
                    image_url=image_url,
                    result_field="captchaEntry",
                )

            def parse_search_page(
                self, response: Response
            ) -> Generator[Any, None, None]:
                yield ParsedData({"body": response.text})

        async with PersistentDriver.open(
            UtahLikeScraper(server_url),
            db_path,
            enable_monitor=False,
            request_preps=[
                WordImageCaptcha(server_url=f"{server_url}/utah-mock/resolve")
            ],
        ) as driver:
            await driver.run(setup_signal_handlers=False)

            async with driver.db._session_factory() as session:
                rows = await session.execute(
                    sa.text("SELECT data_json FROM results ORDER BY id")
                )
                raw = [r[0] for r in rows.all()]

        bodies = [json.loads(r)["body"] for r in raw]
        # Captcha was solved → POST returned the search page, not the
        # "characters did not match" error page.
        assert any("Appellate Case Docket Search" in b for b in bodies), bodies
        assert all("did not match" not in b for b in bodies), bodies
