# Kent Framework API Reference

Condensed reference for writing scrapers. For full details, find the kent
source with `find . -path "*/kent/kent/data_types.py" | head -1` and read
from that directory.

---

## BaseScraper

```python
from kent.data_types import BaseScraper, ScraperStatus

class MyScraper(BaseScraper[OutputType]):
    # Required metadata
    court_ids: ClassVar[set[str]] = {"court_id"}
    court_url: ClassVar[str] = "https://..."
    data_types: ClassVar[set[str]] = {"dockets"}  # or {"opinions"}, {"dockets", "oral_arguments"}
    status: ClassVar[ScraperStatus] = ScraperStatus.IN_DEVELOPMENT  # or ACTIVE, RETIRED
    version: ClassVar[str] = "2026-01-01"
    requires_auth: ClassVar[bool] = False

    # Optional
    rate_limits: ClassVar[list[Rate] | None] = [Rate(1, Duration.SECOND)]
    driver_requirements: ClassVar[list[DriverRequirement]] = []  # Playwright needs
    ssl_context: ClassVar[ssl.SSLContext | None] = None
    oldest_record: ClassVar[date | None] = None
    last_verified: ClassVar[str] = "2026-01-01"
```

### DriverRequirement

When a site needs Playwright:

```python
from kent.data_types import DriverRequirement

class MyScraper(BaseScraper[MyData]):
    driver_requirements = [DriverRequirement.JS_EVAL, DriverRequirement.FF_ALIKE]
```

Common values:

- `JS_EVAL` — needs a JS-evaluating browser engine
- `FF_ALIKE` — needs a Firefox-family engine
- `CHROME_ALIKE` — needs a Chromium-family engine
- `HCAP_HANDLER` — visible hCaptcha widget (`div.h-captcha`); does **not**
  cover invisible / execute-mode hCaptcha (token-as-header) — see the
  patterns appendix in SKILL.md for that case
- `RCAP_HANDLER` — reCAPTCHA

Site-specific values seen in existing scrapers:

- `H11_HEADER_FIXES` — HTTP/1.1 header normalization quirks (Alaska)
- `FOLLOW_REDIRECTS` — explicit redirect-following (Alaska)

The kent source is the canonical list. To see all members:

```bash
grep -A 30 "class DriverRequirement" $(python -c "import kent, os; print(os.path.dirname(kent.__file__))")/data_types.py
```

If the scraper yields multiple top-level types, use a union:
`BaseScraper[Docket | OralArgument]`.

---

## Decorators

### @entry

Marks a method as a scraper entry point. The driver calls these to start
scraping.

```python
from kent.common.decorators import entry

# Basic entry — driver calls with no args
@entry(OutputType)
def get_dockets(self) -> Generator[Request, None, None]:
    yield Request(...)

# Entry with typed parameters
@entry(OutputType)
def get_dockets_by_date(self, date_range: DateRange) -> Generator[Request, None, None]:
    yield Request(...)

# Speculative entry — driver generates sequential IDs
@entry(OutputType)
def fetch_docket(self, case_number: SpeculativeRange) -> Request:
    return Request(...)
```

### @step

Marks a method as a continuation (called when a Request's response arrives).
Parameters are injected by name:

| Parameter | Type | What it provides |
|-----------|------|-----------------|
| `page` | `PageElement` / `LxmlPageElement` | Parsed HTML with query methods |
| `response` | `Response` | HTTP response (status_code, headers, url, text, content) |
| `accumulated_data` | `dict` | Data passed from the parent Request |
| `json_content` | `dict` or `list` | Parsed JSON body |
| `local_filepath` | `str \| None` | Path to archived file (for archive requests) |
| `lxml_tree` | `CheckedHtmlElement` | Raw lxml tree (prefer `page` instead) |
| `text` | `str` | Response body as string |

```python
from kent.common.decorators import step

@step()
def parse_results(self, page: PageElement, response: Response,
                  accumulated_data: dict) -> Generator[ScraperYield, None, None]:
    ...
```

Step options:
- `priority: int = 9` — queue ordering (lower = higher priority)
- `encoding: str = "utf-8"` — text decoding
- `xsd: str | None` — path to XSD schema (structural validation hints)
- `json_model: str | None` — dotted path to Pydantic model for JSON response validation (e.g. `"api.responses.SearchResult"`)
- `auto_await_timeout: int | None` — timeout in ms for Playwright autowait retry logic
- `await_list: list | None` — wait conditions for Playwright (WaitForSelector, WaitForLoadState, WaitForURL, WaitForTimeout)

---

## Response

The `response` parameter in step functions:

| Field | Type | Notes |
|---|---|---|
| `status_code` | `int` | HTTP status code |
| `headers` | `dict[str, str]` | Response headers |
| `url` | `str` | **Final URL after redirects.** Use this to detect redirect-based soft-404s (compare against the requested URL). |
| `text` | `str` | Response body decoded as text |
| `content` | `bytes` | Raw response body bytes |

The Playwright driver follows redirects. The persistent driver (httpx/default) only follows redirects if the FOLLOW_REDIRECTS DriverRequirement is specified.

The kent source is canonical for the full dataclass:

```bash
grep -A 30 "class Response" $(python -c "import kent, os; print(os.path.dirname(kent.__file__))")/data_types.py
```

---

## Request

```python
from kent.data_types import Request, HTTPRequestParams, HttpMethod

# Standard navigating request
yield Request(
    request=HTTPRequestParams(
        method=HttpMethod.GET,  # GET, POST, PUT, DELETE, etc.
        url="https://...",
        params={"key": "value"},        # query string
        data={"field": "value"},         # form POST body
        json={"key": "value"},           # JSON POST body
        headers={"X-Custom": "value"},
        cookies={"session": "abc"},
    ),
    continuation=self.next_step,         # method or string name
    accumulated_data={"key": "value"},   # passed to next step (deep copied)
)

# Non-navigating request (API call, doesn't update current_location)
yield Request(
    request=HTTPRequestParams(method=HttpMethod.GET, url=api_url),
    continuation=self.parse_api,
    nonnavigating=True,
)

# Archive request (downloads and saves file)
yield Request(
    archive=True,
    request=HTTPRequestParams(method=HttpMethod.GET, url=pdf_url),
    continuation=self.handle_download,
    expected_type="pdf",  # or "audio", "image"
    accumulated_data={...},
)

# Deduplication control
from kent.data_types import SkipDeduplicationCheck

yield Request(
    request=HTTPRequestParams(method=HttpMethod.GET, url=next_page_url),
    continuation=self.parse_results,
    deduplication_key=SkipDeduplicationCheck(),  # pagination must always execute
)

# Custom dedup key (prevents visiting same case from overlapping searches)
yield Request(
    request=HTTPRequestParams(method=HttpMethod.GET, url=case_url),
    continuation=self.parse_case,
    deduplication_key=docket_id,
)
```

Additional Request fields:
- `deduplication_key: str | None | SkipDeduplicationCheck` — custom dedup key (auto-generated from URL if None)
- `via: ViaLink | ViaFormSubmit | None` — set automatically by `find_links()` and `form.submit()`
- `bypass_rate_limit: bool = False` — skip rate limiter for this request

---

## PageElement API

The `page` parameter in step functions provides:

```python
# XPath queries — returns list of PageElement
elements = page.query_xpath("//div[@class='case']", "case divs",
                            min_count=0, max_count=None)

# XPath string extraction — returns list of str
texts = page.query_xpath_strings("//td/text()", "cell texts", min_count=0)
hrefs = page.query_xpath_strings("//a/@href", "links", min_count=1)

# CSS queries
elements = page.query_css("div.case", "case divs", min_count=0)

# Text content of an element
text = element.text_content()

# Find a form and submit it
form = page.find_form("//form[@id='search']", "search form")
# form.action — resolved URL
# form.method — GET or POST
# form.fields — list of FormField(name, field_type, value, options)
request = form.submit(data={"field": "value"})
# Returns a Request — set its continuation before yielding

# Find links
links = page.find_links("//a[@class='case-link']", "case links", min_count=0)
# link.url — resolved absolute URL
# link.text — visible text
```

Count validation: if actual count is outside `[min_count, max_count]`, raises
`HTMLStructuralAssumptionException` (caught by driver as a structural error).

---

## ScrapedData (Models)

```python
from kent.common.data_models import ScrapedData

class MyDocket(ScrapedData):
    docket_id: str
    court_id: str
    date_filed: date | None = None
    case_name: str
    entries: list[MyDocketEntry] = []
    source_url: str | None = None
```

### Deferred validation

For yielding data with deferred Pydantic validation:

```python
yield ParsedData(
    MyDocket.raw(
        request_url=response.url,
        docket_id="ABC-123",
        case_name="Foo v. Bar",
        ...
    )
)
```

Or direct construction (immediate validation):

```python
docket = MyDocket(docket_id="ABC-123", case_name="Foo v. Bar", ...)
yield ParsedData(data=docket)
```

---

## Speculation Types

### SpeculativeRange
Single sequential integer parameter:

```python
from kent.common.param_models import SpeculativeRange


@entry(Docket)
def fetch_docket(self, case_number: SpeculativeRange) -> Request:
    ...
```

### YearlySpeculativeRange
Year + sequential number:

```python
from kent.common.param_models import YearlySpeculativeRange


@entry(Docket)
def fetch_docket(self, case_id: YearlySpeculativeRange) -> Request:
    ...
```

---

## Soft-404 Detection

Override on BaseScraper when sites return 200 for missing cases:

```python
def fails_successfully(self, response: Response) -> bool:
    """Return False for soft-404 pages (speculative misses)."""
    return "case not found" not in response.text.lower()
```

---

## ParsedData & EstimateData

```python
from kent.data_types import ParsedData, EstimateData

# Yield final scraped data
yield ParsedData(data=my_model_instance)

# Predict downstream count (integrity checking)
yield EstimateData(
    expected_types=(MyDocket,),
    min_count=len(results),
    max_count=len(results),
)
```

---

## DateRange

```python
from kent.common.param_models import DateRange

# Used as entry point parameter
@entry(Docket)
def get_by_date(self, date_range: DateRange) -> Generator[Request, None, None]:
    start = date_range.start  # date
    end = date_range.end      # date
    ...
```

---

## Form Submission

```python
form = page.find_form("//form[@action='/search']", "search form")

# Inspect form fields
for field in form.fields:
    print(field.name, field.field_type, field.value, field.options)

# submit() accepts **request_kwargs passed to the Request constructor.
# You can pass continuation, accumulated_data, deduplication_key, etc. directly:
yield form.submit(
    data={"query": "smith", "bot_check": "Y"},
    submit_selector="//input[@type='submit']",  # optional
    continuation=self.parse_results,
    accumulated_data=accumulated_data,
)

# Or equivalently, use dataclasses.replace on the returned Request:
from dataclasses import replace
request = form.submit(data={"query": "smith"})
yield replace(request, continuation=self.parse_results)
```

---

## Common Patterns

### accumulated_data serialization

Values in `accumulated_data` must be JSON-serializable (str, int, float,
bool, None, list, dict) because the dict is persisted between requests.
For Pydantic models, use `.model_dump(mode="json")`; for dates, use
`.isoformat()`.

### Chaining tabs via accumulated_data

```python
@step()
def parse_summary(self, page, response, accumulated_data):
    accumulated_data["case_name"] = page.query_xpath_strings(
        "//h1/text()", "case name", min_count=1, max_count=1
    )[0]
    yield Request(
        request=HTTPRequestParams(method=HttpMethod.GET, url=next_tab_url),
        continuation=self.parse_docket_tab,
        accumulated_data=accumulated_data,
    )
```

### Pagination

Use `SkipDeduplicationCheck()` on pagination requests so they always execute:

```python
from kent.data_types import SkipDeduplicationCheck

@step()
def parse_results(self, page, response, accumulated_data):
    # Process current page...
    for row in rows:
        yield Request(...)

    # Check for next page
    next_links = page.find_links(
        "//a[contains(text(), 'Next')]", "next page", min_count=0, max_count=1
    )
    if next_links:
        yield Request(
            request=HTTPRequestParams(method=HttpMethod.GET, url=next_links[0].url),
            continuation=self.parse_results,
            accumulated_data=accumulated_data,
            deduplication_key=SkipDeduplicationCheck(),
        )
```

### URL resolution
URLs from `page.find_links()` and `page.find_form()` are automatically
resolved to absolute URLs based on the response URL. For manual URL building:

```python
from urllib.parse import urljoin
full_url = urljoin(response.url, relative_href)
```
