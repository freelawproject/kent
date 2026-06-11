"""Tests for the single_page() test utility."""

from __future__ import annotations

from collections.abc import Generator

from jkent.common.checked_html import CheckedHtmlElement
from jkent.common.decorators import single_page, step
from jkent.data_types import (
    BaseScraper,
    HttpMethod,
    HTTPRequestParams,
    ParsedData,
    Request,
    Response,
    ScraperYield,
)


class HTMLScraper(BaseScraper[dict]):
    @step
    def parse_page(
        self, lxml_tree: CheckedHtmlElement, response: Response
    ) -> Generator[ScraperYield, None, None]:
        titles = lxml_tree.checked_xpath("//h1", "titles")
        for title in titles:
            yield ParsedData(
                {"title": title.text_content(), "url": response.url}
            )


class JSONScraper(BaseScraper[dict]):
    @step
    def parse_api(
        self, json_content: list
    ) -> Generator[ScraperYield, None, None]:
        for item in json_content:
            yield ParsedData(item)


class AccumulatedDataScraper(BaseScraper[dict]):
    @step
    def parse_detail(
        self, text: str, accumulated_data: dict
    ) -> Generator[ScraperYield, None, None]:
        yield ParsedData(
            {"text": text.strip(), "page": accumulated_data.get("page", 0)}
        )


class MixedYieldScraper(BaseScraper[dict]):
    @step
    def parse_and_follow(
        self, lxml_tree: CheckedHtmlElement, response: Response
    ) -> Generator[ScraperYield, None, None]:
        yield ParsedData({"found": True})
        yield Request(
            request=HTTPRequestParams(
                method=HttpMethod.GET, url="https://example.com/next"
            ),
            continuation="parse_and_follow",
        )
        yield None
        yield ParsedData({"found": True, "second": True})


class TestSinglePageHTML:
    def test_parses_html(self) -> None:
        run = single_page(HTMLScraper, "parse_page")
        results = run("<html><body><h1>Hello</h1></body></html>")
        assert len(results) == 1
        assert results[0]["title"] == "Hello"
        assert results[0]["url"] == "https://test.example.com"

    def test_custom_url(self) -> None:
        run = single_page(HTMLScraper, "parse_page")
        results = run(
            "<html><body><h1>Test</h1></body></html>",
            url="https://court.gov/page",
        )
        assert results[0]["url"] == "https://court.gov/page"

    def test_multiple_results(self) -> None:
        run = single_page(HTMLScraper, "parse_page")
        results = run(
            "<html><body><h1>A</h1><h1>B</h1><h1>C</h1></body></html>"
        )
        assert len(results) == 3
        assert [r["title"] for r in results] == ["A", "B", "C"]


class TestSinglePageJSON:
    def test_parses_json(self) -> None:
        run = single_page(JSONScraper, "parse_api")
        results = run('[{"id": 1}, {"id": 2}]')
        assert results == [{"id": 1}, {"id": 2}]


class TestSinglePageAccumulatedData:
    def test_passes_accumulated_data(self) -> None:
        run = single_page(AccumulatedDataScraper, "parse_detail")
        results = run("some content", accumulated_data={"page": 3})
        assert results[0]["page"] == 3

    def test_default_empty_accumulated_data(self) -> None:
        run = single_page(AccumulatedDataScraper, "parse_detail")
        results = run("some content")
        assert results[0]["page"] == 0


class TestSinglePageMixedYields:
    def test_filters_non_parsed_data(self) -> None:
        run = single_page(MixedYieldScraper, "parse_and_follow")
        results = run("<html><body></body></html>")
        assert len(results) == 2
        assert results[0] == {"found": True}
        assert results[1] == {"found": True, "second": True}
