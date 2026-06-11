"""Tests for BaseScraper class attributes and introspection methods.

This test module verifies:
- BaseScraper class attribute defaults (court_ids, status, version, etc.)
- Subclass overrides of class attributes
- ScraperStatus enum values
- list_steps() discovers @step-decorated methods
- get_params() returns the params instance
- get_ssl_context() returns SSL context or None
"""

import ssl
from collections.abc import Generator
from datetime import date

from pyrate_limiter import Duration, Rate

from jkent.common.decorators import entry, step
from jkent.data_types import (
    BaseScraper,
    HttpMethod,
    HTTPRequestParams,
    ParsedData,
    Request,
    Response,
    ScraperStatus,
    ScraperYield,
    StepInfo,
)

# =============================================================================
# Test Scrapers
# =============================================================================


class BareMinimumScraper(BaseScraper[dict]):
    """Scraper with no overrides — uses all defaults."""

    @entry(dict)
    def get_entry(self) -> Generator[Request, None, None]:
        yield Request(
            request=HTTPRequestParams(method=HttpMethod.GET, url="/start"),
            continuation="parse",
        )

    @step
    def parse(self, response: Response) -> Generator[ScraperYield, None, None]:
        yield ParsedData({"ok": True})


class FullyConfiguredScraper(BaseScraper[dict]):
    """Scraper with all class attributes overridden."""

    court_ids = {"cand", "cacd"}
    court_url = "https://ecf.cand.uscourts.gov"
    data_types = {"opinions", "dockets"}
    status = ScraperStatus.ACTIVE
    version = "2025-06-01"
    last_verified = "2025-06-15"
    oldest_record = date(2000, 1, 1)
    requires_auth = True
    rate_limits = [Rate(10, Duration.SECOND)]

    @entry(dict)
    def get_entry(self) -> Generator[Request, None, None]:
        yield Request(
            request=HTTPRequestParams(method=HttpMethod.GET, url="/start"),
            continuation="parse",
        )

    @step
    def parse(self, response: Response) -> Generator[ScraperYield, None, None]:
        yield ParsedData({"ok": True})


class MultiStepScraper(BaseScraper[dict]):
    """Scraper with multiple @step methods at different priorities."""

    @entry(dict)
    def get_entry(self) -> Generator[Request, None, None]:
        yield Request(
            request=HTTPRequestParams(method=HttpMethod.GET, url="/start"),
            continuation="parse_listing",
        )

    @step
    def parse_listing(
        self, response: Response
    ) -> Generator[ScraperYield, None, None]:
        yield ParsedData({"page": "listing"})

    @step(priority=5)
    def parse_detail(
        self, response: Response
    ) -> Generator[ScraperYield, None, None]:
        yield ParsedData({"page": "detail"})

    @step(priority=1, encoding="latin-1")
    def parse_document(
        self, response: Response
    ) -> Generator[ScraperYield, None, None]:
        yield ParsedData({"page": "document"})

    def not_a_step(self):
        """Plain method — should not appear in list_steps()."""
        pass


class CustomSSLScraper(BaseScraper[dict]):
    """Scraper with a custom SSL context."""

    @classmethod
    def get_ssl_context(cls) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        return ctx

    @entry(dict)
    def get_entry(self) -> Generator[Request, None, None]:
        yield Request(
            request=HTTPRequestParams(method=HttpMethod.GET, url="/start"),
            continuation="parse",
        )

    @step
    def parse(self, response: Response) -> Generator[ScraperYield, None, None]:
        yield ParsedData({"ok": True})


# =============================================================================
# ScraperStatus Enum
# =============================================================================


class TestScraperStatus:
    """Test ScraperStatus enum values."""

    def test_enum_values(self):
        assert ScraperStatus.IN_DEVELOPMENT.value == "in_development"
        assert ScraperStatus.ACTIVE.value == "active"
        assert ScraperStatus.RETIRED.value == "retired"

    def test_all_members(self):
        assert set(ScraperStatus) == {
            ScraperStatus.IN_DEVELOPMENT,
            ScraperStatus.ACTIVE,
            ScraperStatus.RETIRED,
        }


# =============================================================================
# BaseScraper Class Attribute Defaults
# =============================================================================


class TestBaseScraperDefaults:
    """Test that BaseScraper class attributes have correct defaults."""

    def test_court_ids_defaults_to_empty_set(self):
        assert BareMinimumScraper.court_ids == set()

    def test_court_url_defaults_to_empty_string(self):
        assert BareMinimumScraper.court_url == ""

    def test_data_types_defaults_to_empty_set(self):
        assert BareMinimumScraper.data_types == set()

    def test_status_defaults_to_in_development(self):
        assert BareMinimumScraper.status == ScraperStatus.IN_DEVELOPMENT

    def test_version_defaults_to_empty_string(self):
        assert BareMinimumScraper.version == ""

    def test_last_verified_defaults_to_empty_string(self):
        assert BareMinimumScraper.last_verified == ""

    def test_oldest_record_defaults_to_none(self):
        assert BareMinimumScraper.oldest_record is None

    def test_requires_auth_defaults_to_false(self):
        assert BareMinimumScraper.requires_auth is False

    def test_rate_limits_defaults_to_none(self):
        assert BareMinimumScraper.rate_limits is None

    def test_ssl_context_defaults_to_none(self):
        assert BareMinimumScraper.ssl_context is None


# =============================================================================
# BaseScraper Class Attribute Overrides
# =============================================================================


class TestBaseScraperOverrides:
    """Test that subclass overrides work correctly."""

    def test_court_ids_override(self):
        assert FullyConfiguredScraper.court_ids == {"cand", "cacd"}

    def test_court_url_override(self):
        assert (
            FullyConfiguredScraper.court_url == "https://ecf.cand.uscourts.gov"
        )

    def test_data_types_override(self):
        assert FullyConfiguredScraper.data_types == {"opinions", "dockets"}

    def test_status_override(self):
        assert FullyConfiguredScraper.status == ScraperStatus.ACTIVE

    def test_version_override(self):
        assert FullyConfiguredScraper.version == "2025-06-01"

    def test_last_verified_override(self):
        assert FullyConfiguredScraper.last_verified == "2025-06-15"

    def test_oldest_record_override(self):
        assert FullyConfiguredScraper.oldest_record == date(2000, 1, 1)

    def test_requires_auth_override(self):
        assert FullyConfiguredScraper.requires_auth is True

    def test_rate_limits_override(self):
        assert FullyConfiguredScraper.rate_limits is not None
        assert len(FullyConfiguredScraper.rate_limits) == 1

    def test_overrides_do_not_affect_base_class(self):
        """Subclass overrides should not leak into BaseScraper defaults."""
        assert BaseScraper.court_ids == set()
        assert BaseScraper.status == ScraperStatus.IN_DEVELOPMENT
        assert BaseScraper.requires_auth is False


# =============================================================================
# get_params()
# =============================================================================


class TestGetParams:
    """Test BaseScraper.get_params()."""

    def test_get_params_returns_none_by_default(self):
        scraper = BareMinimumScraper()
        assert scraper.get_params() is None

    def test_get_params_returns_provided_params(self):
        params = {"court": "cand", "year": 2025}
        scraper = BareMinimumScraper(params=params)
        assert scraper.get_params() == params

    def test_get_params_returns_arbitrary_object(self):
        class MyParams:
            court = "cand"

        p = MyParams()
        scraper = BareMinimumScraper(params=p)
        assert scraper.get_params() is p


# =============================================================================
# get_ssl_context()
# =============================================================================


class TestGetSSLContext:
    """Test BaseScraper.get_ssl_context()."""

    def test_default_returns_none(self):
        assert BareMinimumScraper.get_ssl_context() is None

    def test_custom_returns_ssl_context(self):
        ctx = CustomSSLScraper.get_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)

    def test_ssl_context_classvar_returned_when_set(self):
        """If ssl_context ClassVar is set directly, get_ssl_context() returns it."""

        class DirectSSLScraper(BaseScraper[dict]):
            ssl_context = ssl.create_default_context()

            @entry(dict)
            def get_entry(self) -> Generator[Request, None, None]:
                yield Request(
                    request=HTTPRequestParams(
                        method=HttpMethod.GET, url="/start"
                    ),
                    continuation="parse",
                )

            @step
            def parse(
                self, response: Response
            ) -> Generator[ScraperYield, None, None]:
                yield ParsedData({"ok": True})

        ctx = DirectSSLScraper.get_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx is DirectSSLScraper.ssl_context


# =============================================================================
# list_steps()
# =============================================================================


class TestListSteps:
    """Test BaseScraper.list_steps() introspection."""

    def test_discovers_all_step_methods(self):
        steps = MultiStepScraper.list_steps()
        step_names = {s.name for s in steps}
        assert step_names == {
            "parse_listing",
            "parse_detail",
            "parse_document",
        }

    def test_returns_step_info_objects(self):
        steps = MultiStepScraper.list_steps()
        assert all(isinstance(s, StepInfo) for s in steps)

    def test_step_priority_metadata(self):
        steps = MultiStepScraper.list_steps()
        by_name = {s.name: s for s in steps}

        assert by_name["parse_listing"].priority == 9  # default
        assert by_name["parse_detail"].priority == 5
        assert by_name["parse_document"].priority == 1

    def test_step_encoding_metadata(self):
        steps = MultiStepScraper.list_steps()
        by_name = {s.name: s for s in steps}

        assert by_name["parse_listing"].encoding == "utf-8"  # default
        assert by_name["parse_document"].encoding == "latin-1"

    def test_excludes_non_step_methods(self):
        steps = MultiStepScraper.list_steps()
        step_names = {s.name for s in steps}
        assert "not_a_step" not in step_names
        assert "get_entry" not in step_names

    def test_empty_scraper_returns_empty_list(self):
        """Scraper with no @step methods returns an empty list."""

        class NoStepScraper(BaseScraper[dict]):
            @entry(dict)
            def get_entry(self) -> Generator[Request, None, None]:
                yield Request(
                    request=HTTPRequestParams(
                        method=HttpMethod.GET, url="/start"
                    ),
                    continuation="process",
                )

            def process(self, response: Response):
                """Not decorated with @step."""
                pass

        steps = NoStepScraper.list_steps()
        assert steps == []
