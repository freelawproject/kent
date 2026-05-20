---
name: design-scraper
description: Design and implement a kent scraper for an appellate court website. Invoked with a URL to explore. Uses Playwright for site reconnaissance, then produces DESIGN.md, models.py, and scraper.py.
user-invocable: true
argument-hint: <url>
---

# Design Scraper

You are designing and implementing a kent framework scraper for an appellate
court website. The user provides a URL as the argument to this skill.

See [kent-api-reference.md](kent-api-reference.md) for the kent framework API.

## Output Files

Locate the juriscraper scrapers directory. **Filter out the `build/` tree** —
a stale wheel-build copy may shadow the canonical source path; writing files
into it silently puts them in a build artefact that nobody imports:

```bash
find . -path "*/juriscraper/sd/state" -type d 2>/dev/null \
    | grep -v '/build/' | head -1
```

All files go under that directory at `{state}/{domain_underscored}/`:

- `DESIGN.md` — Site analysis and design decisions
- `models.py` — Pydantic data models (ScrapedData subclasses)
- `scraper.py` — Scraper implementation (BaseScraper subclass)
- `__init__.py` — Empty package init

Derive `{state}` from the court's US state (lowercase, underscores:
`california`, `new_york`).

Derive `{domain_underscored}` from the hostname:
1. Strip a leading `www.` if present.
2. Replace **both dots and hyphens** with underscores (Python module names
   cannot contain hyphens).

Examples: `appellatecases.courtinfo.ca.gov` → `appellatecases_courtinfo_ca_gov`,
`ma-appellatecourts.org` → `ma_appellatecourts_org`,
`e-courts.judicial.state.al.us` → `e_courts_judicial_state_al_us`.

If the target directory already exists, read existing files before overwriting.

---

## Phase 1: Site Reconnaissance

1. **Navigate** to the URL with `browser_navigate`.
2. **Snapshot** the page (`browser_snapshot`) to see forms, links, layout.
3. **Identify all search forms** — note each form's action URL, method
   (GET/POST), and every field (name, type, required, options for selects).

   *If `browser_snapshot` shows no `<form>` element on a page that obviously
   has a search form (common on SPAs), fall back to `browser_evaluate`:*
   ```js
   const inputs = document.querySelectorAll('input, select');
   return Array.from(inputs).map(el => ({
       name: el.name, type: el.type, value: el.value
   }));
   ```
   *The accessibility-tree snapshot sometimes drops forms that lack `id` /
   `name` / `aria-*` attributes on the `<form>` tag itself.*
4. **Identify all courts covered** — look for:
   - Court selector dropdowns or radio buttons
   - URL parameters (e.g., `dist=3`, `court=SC`)
   - Separate pages per court

   Record every court's internal identifier, display name, and any division
   info.
5. **Check for a calendar / oral arguments section** — often has date-based
   search even when the main case search doesn't. If found, note the URL and
   search fields.

   *Try navigating to a prior month — some sites' calendars redirect any
   historical URL pattern back to `/`. If no past-month URL works, the
   calendar is a snapshot-only resource and a dateless `@entry` is the only
   option.*

---

## Phase 1.5: Find Canonical Sibling Exemplars

Based on Phase 1 findings, identify 1–3 candidate sibling scrapers from the
exemplars table below. Read their `scraper.py` and `models.py` for structural
reference — imports, decorator argument styles, helper conventions — before
writing your own. Revisit after Phase 4 if the technical assessment shifts
the picture (e.g. the site turns out to be a JSON SPA you didn't catch in
Phase 1).

| Site shape | Canonical exemplar | Why |
|---|---|---|
| `YearlySpeculativeRange` (year-partitioned) | [georgia/gaappeals_gov](../../../juriscraper/sd/state/georgia/gaappeals_gov/scraper.py) | Five `@entry` per case-letter; year-aware ID formatting |
| `SpeculativeRange` (continuous integer) | [alaska/appellate_records_courts_alaska_gov](../../../juriscraper/sd/state/alaska/appellate_records_courts_alaska_gov/scraper.py) | Two `@entry`, no year axis, simplest shape |
| Multi-prefix `@entry` cluster + shared helper | [maryland/casesearch_courts_state_md_us](../../../juriscraper/sd/state/maryland/casesearch_courts_state_md_us/scraper.py) | Five entries (ACM-REG, ACM-ALA, SCM-PET, SCM-MISC, SCM-REG) all delegating to `_build_speculative_request` |
| JSON API w/ `json_content` step | [washington/acdocportal_courts_wa_gov](../../../juriscraper/sd/state/washington/acdocportal_courts_wa_gov/scraper.py) | Uses `@step(json_model=...)` for validated JSON parsing |
| Date-search HTML form | [new_york/nycourts_gov](../../../juriscraper/sd/state/new_york/nycourts_gov/scraper.py) | DateRange-driven ASP.NET WebForms POST with paginated table results |
| Single-page RSI / Public Access portal | [massachusetts/ma_appellatecourts_org](../../../juriscraper/sd/state/massachusetts/ma_appellatecourts_org/scraper.py) | One GET returns full case file inline as `<section>` blocks |
| Episerver SSR JSON | [michigan/courts_michigan_gov](../../../juriscraper/sd/state/michigan/courts_michigan_gov/scraper.py) | Hits page URL with `?expand=*&currentPageUrl=...` for SSR'd page object |
| Newest-sorted listing walk | [michigan/courts_michigan_gov](../../../juriscraper/sd/state/michigan/courts_michigan_gov/scraper.py) | No date filter — walks `sortOrder=Newest` and stops on window boundary |
| Redirect-based soft-404 | [massachusetts/ma_appellatecourts_org](../../../juriscraper/sd/state/massachusetts/ma_appellatecourts_org/scraper.py) | `fails_successfully` checks `response.url`, not body text |
| 4xx-as-speculative-miss | [maryland/casesearch_courts_state_md_us](../../../juriscraper/sd/state/maryland/casesearch_courts_state_md_us/scraper.py) | API returns HTTP 400 for invalid IDs; no `fails_successfully` override needed |

---

## Phase 2: Probe Search Interfaces

**Before probing, identify the transport.** Watch the network panel during
a manual search. If you see a `fetch()` returning JSON, target it directly —
don't reverse-engineer the rendered HTML. Look up the canonical sibling in
the exemplars table for either shape. The transport choice directly shapes
the scraper's `@step` signatures (`json_content: dict` vs `page: PageElement`).

### Pick a bulk scraping strategy (decision tree)

1. **Site has a usable date-range filter?** → date-based search. Best option.
2. **No date filter, but listing endpoint shows filing dates and supports
   forward pagination with no result-count cap?** → **newest-sorted listing
   walk** (see Michigan as exemplar — `sortOrder=Newest`, walk pages until
   the oldest in-window item < window start).
3. **Otherwise** → **speculative entry on case numbers** (see Alaska / Georgia /
   Maryland as exemplars depending on shape).

Probing (party search etc.) is a *discovery* technique — see "Always probe"
below — not a bulk scraping strategy.

### If date search exists — test it
- Submit a 7-day window for a recent period.
- Note date field format (`mm/dd/yyyy`, ISO, etc.).
- Check result count caps (some APIs cap at 10,000).
- Check pagination (GET params, POST body, JS-driven).

### JSON-API target

If search returns JSON instead of HTML, the step uses `json_content` instead
of `page`:

```python
@step()
def parse_search_results(
    self,
    json_content: dict,
    response: Response,
    accumulated_data: dict,
) -> Generator[ScraperYield[MyDocket], None, None]:
    for record in json_content.get("results", []):
        ...
```

- Set `headers={"Accept": "application/json"}` on the request.
- **Inspect the JSON shape before assuming pagination is needed.** Many APIs
  dump all results in a single response even when the UI paginates client-side
  (e.g. Maryland returns all 94 results at once).
- For SPA sites with bot protection, hitting the JSON API *through Playwright*
  (after the JS challenge sets cookies) is faster and more robust than
  scraping the rendered DOM. See Phase 4.
- Episerver / Optimizely sites can have a special SSR variant —
  see the patterns appendix entry for `?expand=*&currentPageUrl=...`.
- Listing endpoints sometimes expose multiple result containers selectable
  by a `resultType=` parameter — see the patterns appendix.

### If case number search exists — test it
- Try a known number (user may provide one, or find one via party search).
- Note whether it redirects directly to the case or shows a results list.
- Note any **bot protection / framework CSRF fields** — hidden inputs
  auto-set by JavaScript or by the server-rendered form. Common conventions:
  `_token` (Laravel), `__RequestVerificationToken` (ASP.NET Core),
  `csrfmiddlewaretoken` (Django). The simplest get-out-of-jail-free path is
  `page.find_form().submit(...)` — it preserves all hidden fields
  automatically rather than requiring you to enumerate them.

### Always probe — party name (regardless of strategy chosen)
Search for **"smith"** to discover:
- **Docket number format** per court — prefix, sequential digits, year
  component. Examples: `C000125` (letter + digits), `SC-2023-0123`
  (court-year-seq), `2024-00003` (year-seq).
- **Result count** and pagination behavior.
- Whether trial court numbers appear alongside appellate numbers.

Search in multiple courts if the site covers more than one, since different
courts often use different docket number prefixes.

### Speculative entry assessment

If the decision tree lands you on speculative entry, determine:
- The docket number pattern per court (prefix + sequential number).
- The approximate range (lowest and highest numbers observed).
- The largest gaps between sequential numbers.
- Whether numbers reset yearly or are continuous.

#### Continuous numbers — `SpeculativeRange`
- Example: District 3 uses prefix `C` + up to 6 digits, highest observed
  `C105926` → use `SpeculativeRange` as the entry parameter type and seed
  with `{"number": 105926, "gap": 20}`.

#### Year-partitioned numbers — `YearlySpeculativeRange`
For year-partitioned numbers (e.g. `2024-00003`):
- Use `YearlySpeculativeRange` as the entry parameter type.
- Seed shape is `{"year": YYYY, "min": N, "soft_max": M, "gap": K}`.
- One seed entry per year. Example seed_params:
  ```python
  [
      {"fetch_case": {"case_id": {"year": 2024, "min": 1, "soft_max": 4000, "gap": 0}}},
      {"fetch_case": {"case_id": {"year": 2025, "min": 1, "soft_max": 1, "gap": 15}}},
  ]
  ```
- **Year rollover is operational, not a scraper concern.** The seed_params
  author (operator) is responsible for adding a new year's entry at year
  rollover. The scraper itself does not enumerate years.

#### Multiple type prefixes — one `@entry` per prefix
If a site uses multiple case-number prefixes that each have their own
sequence (e.g. by case type — Maryland's ACM-REG, ACM-ALA, SCM-PET,
SCM-MISC, SCM-REG; Georgia's A, D, E, I, O), declare **one `@entry` per
prefix** and share a `_build_speculative_request(case_id, prefix_args)`
helper. The driver advances each prefix's sequence independently. See
Maryland as the canonical exemplar.

---

## Phase 3: Explore Case Details

**Before clicking through tabs, identify the detail shape.** Snapshot a
representative case page. If every field you need (header, parties, docket,
documents) is already on one page in `<section>` blocks with no AJAX-loaded
sub-resources, you're in **single-page-portal territory** (RSI / older
Public-Access systems — Massachusetts is the exemplar). Design for one
`parse_case_detail` step rather than a tab chain. Otherwise, the multi-tab
walkthrough below applies.

For multi-tab cases: click into a case result and visit **every** available
tab or section. For each tab:

1. Note the **URL pattern** and what parameters are needed (session tokens,
   doc IDs, etc.).
2. Record **every data field** displayed.
3. Check for **downloadable documents** (PDFs, audio, images).
4. Note whether content is server-rendered or loaded via JavaScript/AJAX.

### Standard tabs to look for

| Tab | Key fields |
|-----|-----------|
| Case Summary | Case type, filing date, completion date, caption, division |
| Docket / Register of Actions | Date, description, notes per entry |
| Briefs | Brief type, due date, filed date, party/attorney |
| Disposition | Outcome, date, publication status, author, citation |
| Parties & Attorneys | Names, roles, firms, addresses, phone numbers |
| Trial Court | Court name, county, case number, judge, judgment date |
| Scheduled Actions | Future events, hearing dates |
| Documents | Download links with types, dates, descriptions |

### Email notifications
Look for "subscribe to email notifications" or similar links on case pages.
If found:
- Note the URL pattern.
- Record all available notification event types (e.g. "Brief Filed",
  "Disposition", "Opinion Available Online").
- Document this in DESIGN.md.

---

## Phase 4: Technical Assessment

### Per-endpoint probing

Test each endpoint you'll need with curl to build a per-endpoint protection
map:

```bash
curl -s -o /dev/null -w "%{http_code}\n" "URL"
curl -s "URL" | head -50
```

- **Full server-rendered HTML** or **clean JSON response** → that endpoint
  works over httpx.
- **CloudFlare challenge page**, captcha challenge, or empty body → that
  endpoint needs Playwright.

Mixed protection is common: listing/search endpoints are often open even
when per-record endpoints are gated.

### Decide once for the whole scraper

`driver_requirements` is a scraper-wide ClassVar. The scraper is either
httpx **or** Playwright — not both. If any endpoint you need requires
Playwright, the whole scraper is Playwright; otherwise pure httpx.

The key driver of Playwright requirement is **bot protection** (CloudFlare,
Akamai, DataDome, etc.), not the server framework. ASP.NET, ColdFusion, PHP,
Episerver sites all work fine with httpx when there is no JS challenge gate.

### When Playwright is the choice and a JSON API exists

Hit the JSON API directly **through** the Playwright context — don't scrape
the rendered DOM and don't try to call the API via httpx with stolen cookies.
Playwright handles the JS challenge and obtains the bot-protection cookie;
subsequent API calls go through cleanly. This is faster and more robust than
DOM scraping.

### Mixed-protection fallback

If the listing is open over HTTP but per-record detail is hard-gated (e.g.
invisible hCaptcha — see patterns appendix), it's reasonable to ship a v1
that yields listing-only records with `status=IN_DEVELOPMENT` and a
DESIGN.md gap note, rather than block on captcha integration.

### Court ID mapping

Look up each court in CourtListener's database. Find `courts.json` by
searching for it:

```bash
find ../.. -path "*/courts_db/data/courts.json" 2>/dev/null | head -1
```

Each entry has:
- `id` — CourtListener court ID (e.g. `calctapp3d`)
- `name` — Full court name
- `type` — `appellate`, `trial`, etc.
- `level` — `colr` (court of last resort), `iac` (intermediate appellate)
- `parent` — Parent court ID for sub-courts

Search by state name and court name. Build a mapping: **site internal ID →
display name → CourtListener court ID**.

For courts with divisions that map to a single CourtListener ID (e.g.
CA District 4 Divisions 1-3 all map to `calctapp4d`), note this in the
mapping.

---

## Phase 5: Write DESIGN.md

```markdown
# {Site Name} Scraper Design

## Site Overview
- **Base URL**: {url}
- **Requires Playwright**: {Yes — CloudFlare / No — server-rendered HTML}
- **Transport**: {HTML form / JSON API / single-page portal / Episerver SSR}

## Courts Covered

| Site ID | Display Name | CourtListener ID |
|---------|-------------|-----------------|
| ... | ... | ... |

## Search Capabilities
{Decision-tree result with notes on each available mode}
**Recommended approach**: {date-based / newest-walk / speculative / hybrid}

## Docket Number Formats
{Per court: prefix pattern, sequential component, year component, examples}

## Data Available

### Case Summary
{List every field with its type}

### Docket Entries
{fields}

### Briefs
{fields}

### Disposition
{fields}

### Parties & Attorneys
{fields}

### Trial Court
{fields}

### Documents
{fields}

## Email Notifications
{Available / Not available}
{If available: URL pattern, event types, registration fields}

## Oral Arguments Calendar
{Available / Not available}
{If available: search modes, fields, current-month-only caveat if applicable}

## Bot Protection Notes
{Hidden fields, session tokens, cookie requirements, redirect behavior}

## Known Gaps (if shipping listing-only v1)
{e.g. invisible hCaptcha on per-case detail; listing only currently}

## Scraper Architecture

### Entry Points
{List each @entry function with its type, params, and purpose}

### Step Functions
{Flow: entry → step1 → step2 → ... → ParsedData}

### Models
{List of ScrapedData models to create}
```

---

## Phase 6: Write models.py

Import `ScrapedData` from `kent.common.data_models`. Follow these conventions:

- Every model extends `ScrapedData`.
- Use type hints: `str`, `date`, `int`, `list[X]`, `X | None`.
- Add a docstring on every field.
- Default optional fields to `None`; default lists to `[]`.
- Prefer `str | None = None` over `Optional[str]`.
- Use `date` (not `datetime`) for date fields.
- Include a `COURT_IDS` dict mapping CourtListener IDs to display names.
  (For some scraper shapes this dict is documentation only — speculative
  entry methods know their own court IDs without needing to look it up.
  Keep it anyway for human reference.)
- Include any site-specific config (API endpoints, court internal IDs, etc.).

### Standard model hierarchy for docket scrapers

```python
class {Prefix}DocketEntry(ScrapedData):
    """A single entry from the Register of Actions / Docket tab."""
    date_filed: date | None = None
    description: str
    notes: str | None = None

class {Prefix}Party(ScrapedData):
    """A party in the case."""
    name: str
    role: str  # e.g., "Plaintiff and Appellant"
    attorneys: list[{Prefix}Attorney] = []

class {Prefix}Attorney(ScrapedData):
    """Attorney representation record."""
    name: str
    firm: str | None = None
    address: str | None = None
    phone: str | None = None

class {Prefix}Document(ScrapedData):
    """A downloadable document from the case."""
    download_url: str
    document_type: str
    date_filed: date | None = None
    description: str | None = None
    local_path: str | None = None

class {Prefix}Docket(ScrapedData):
    """Main output — a complete appellate case docket."""
    # Searchable fields
    docket_id: str
    court_id: str
    date_filed: date | None = None
    case_name: str
    # Case metadata
    case_type: str | None = None
    ...
    # Nested data
    entries: list[{Prefix}DocketEntry] = []
    parties: list[{Prefix}Party] = []
    documents: list[{Prefix}Document] = []
    source_url: str | None = None
```

### Modeling future-calendar / scheduled-hearing entries

If a case-detail page has a "Future Calendar" or "Scheduled Hearings" section
previewing upcoming sittings for that case, **model each item as a
`{Prefix}DocketEntry` instance on the docket** — *not* as a separate
`{Prefix}ScheduledHearing` or `{Prefix}Hearing` model. Future-calendar items
are conceptually one more row in the register of actions; splitting them
into a parallel type creates a parallel data path that downstream consumers
have to reconcile for no benefit.

(Per-court calendar *pages* — a separate page listing all sittings for a
court in a month — are a separate question. If the site has both, the
per-case items go in the docket as DocketEntry; the per-court calendar is
its own decision and may warrant a top-level `{Prefix}OralArgument` type
with its own entry point.)

### Other models

Add more models as the site warrants:
- `{Prefix}Brief` — if briefs tab has structured columns beyond docket entries
- `{Prefix}Disposition` — if disposition has multiple structured fields
- `{Prefix}TrialCourtInfo` — embedded in the main Docket rather than separate
- `{Prefix}OralArgument` — if oral arguments are a separate data type with
  their own entry point (per-court calendar pages, *not* per-case future
  calendar)

---

## Phase 7: Write scraper.py

### Imports

```python
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urljoin

from kent.common.decorators import entry, step
from kent.common.exceptions import TransientException
from kent.common.page_element import PageElement
from kent.common.param_models import DateRange, SpeculativeRange
from kent.data_types import (
    BaseScraper,
    DriverRequirement,
    HttpMethod,
    HTTPRequestParams,
    ParsedData,
    Request,
    Response,
    ScraperStatus,
    SkipDeduplicationCheck,
)
from pyrate_limiter import Duration, Rate

from .models import ...  # Import your models

if TYPE_CHECKING:
    from collections.abc import Generator
    from kent.data_types import ScraperYield
```

### Class metadata

```python
class {Name}Scraper(BaseScraper[{MainType}]):
    """Scraper for {Court Name(s)}.

    {Brief description of what's scraped and how.}
    """
    court_ids: ClassVar[set[str]] = {"id1", "id2", ...}
    court_url: ClassVar[str] = "https://..."
    data_types: ClassVar[set[str]] = {"dockets"}  # or {"dockets", "oral_arguments"}
    status: ClassVar[ScraperStatus] = ScraperStatus.IN_DEVELOPMENT
    version: ClassVar[str] = "{YYYY-MM-DD}"
    requires_auth: ClassVar[bool] = False
    rate_limits: ClassVar[list[Rate] | None] = [Rate(1, Duration.SECOND)]
    # Only if Playwright is needed (bot protection, JS SPA):
    # driver_requirements: ClassVar[list[DriverRequirement]] = [
    #     DriverRequirement.JS_EVAL, DriverRequirement.FF_ALIKE,
    # ]
```

If the scraper yields multiple top-level types, the generic parameter should
be their union: `BaseScraper[Docket | OralArgument]`.

### Entry point strategy

**Date-based search available:**
```python
@entry({Docket})
def get_dockets(self) -> Generator[Request, None, None]:
    """Fetch dockets using date range from scraper params."""
    date_gte, date_lte = self._get_date_params()
    yield Request(
        request=HTTPRequestParams(method=HttpMethod.GET, url=SEARCH_URL),
        continuation=self.parse_search_page,
        accumulated_data={"date_gte": ..., "date_lte": ...},
    )

@entry({Docket})
def get_dockets_by_date(self, date_range: DateRange) -> Generator[Request, None, None]:
    """Fetch dockets for an explicit date range."""
    ...
```

**Newest-sorted listing walk** (no date filter — see Michigan exemplar):
```python
@entry({Docket})
def get_dockets_by_date(self, date_range: DateRange) -> Generator[Request, None, None]:
    """Walk newest-first listing until oldest-on-page < date_range.start."""
    yield Request(
        request=HTTPRequestParams(
            method=HttpMethod.GET,
            url=LISTING_URL,
            params={"sortOrder": "Newest", "page": 1, "pageSize": 100},
        ),
        continuation=self.parse_listing_page,
        accumulated_data={
            "date_gte": date_range.start.isoformat(),
            "date_lte": date_range.end.isoformat(),
            "page": 1,
        },
    )
```
The step then enqueues the next page only if the oldest in-window item on
the current page is still ≥ `date_gte`.

**Speculative entry (one per court):** the driver detects speculation via
the parameter type — no decorator argument needed.
```python
@entry({Docket})
def fetch_{court_prefix}_docket(self, rid: SpeculativeRange) -> Request:
    """Speculative docket fetcher for {Court Name}."""
    docket_id = f"{PREFIX}{rid.min:06d}"  # Format to match site pattern
    return Request(
        request=HTTPRequestParams(
            method=HttpMethod.POST,
            url=SEARCH_URL,
            data={"query_caseNumber": docket_id, ...},
        ),
        continuation=self.parse_search_results,
        accumulated_data={"court_id": "...", "docket_id": docket_id},
    )
```

For year-partitioned numbers use `YearlySpeculativeRange` as the parameter
type (provides `.year` and `.min`). Seed params use `min`, optional
`soft_max`, `should_advance`, and `gap` — see Phase 2's speculative section
for the seed shape and year-rollover responsibility.

For sites with multiple case-type prefixes, see the multi-prefix pattern
under Phase 2's speculative entry assessment — Maryland is the canonical
exemplar.

**Oral arguments (if discovered):**
```python
@entry({OralArgument})
def get_oral_arguments_by_date(self, date_range: DateRange) -> Generator[Request, None, None]:
    """Fetch oral arguments for a date range."""
    ...
```

### Step functions

Each step function:
- Accepts injected parameters by name: `page` (PageElement), `response`
  (Response), `accumulated_data` (dict), `json_content` (dict / list),
  `text` (str), `local_filepath` (str | None).
- Uses `page.query_xpath()`, `page.find_form()`, `page.find_links()` for
  HTML parsing. For JSON APIs, takes `json_content` instead.
- Yields `Request` for follow-on pages and `ParsedData` for final output.
- Passes context forward via `accumulated_data`.
- Values in `accumulated_data` must be JSON-serializable. Use
  `.model_dump(mode="json")` for Pydantic models, `.isoformat()` for dates.

**Typical flow for a multi-tab case detail scraper:**
```
entry (search) → parse_search_results → parse_case_summary
                                       → parse_docket_entries
                                       → parse_parties
                                       → parse_disposition
                                       → parse_trial_court
                                       → assemble_docket (yields ParsedData)
```

**Typical flow for a single-page-portal scraper** (Massachusetts):
```
entry (search) → parse_search_results → parse_case_detail (yields ParsedData)
```

For sites where all tabs are separate pages, chain them via accumulated_data,
collecting fields as you go:
```python
@step()
def parse_case_summary(self, page: PageElement, response: Response,
                       accumulated_data: dict) -> Generator[...]:
    # Extract case summary fields
    accumulated_data["case_name"] = ...
    accumulated_data["case_type"] = ...
    # Yield request for next tab
    yield Request(
        request=HTTPRequestParams(method=HttpMethod.GET, url=docket_tab_url),
        continuation=self.parse_docket_entries,
        accumulated_data=accumulated_data,
    )
```

For the final step, assemble and yield the complete model:
```python
@step()
def assemble_docket(self, accumulated_data: dict) -> Generator[...]:
    docket = {Prefix}Docket(
        docket_id=accumulated_data["docket_id"],
        court_id=accumulated_data["court_id"],
        ...
    )
    yield ParsedData(data=docket)
```

### Soft-404 detection

Run a curl on a known-bad ID to diagnose the shape, then route to the right
detector:

```bash
curl -s -o /dev/null -w "%{http_code}\n" "URL_WITH_BAD_ID"
curl -sL -o /dev/null -w "%{url_effective}\n" "URL_WITH_BAD_ID"
```

| Symptom | Detector |
|---|---|
| HTTP 4xx (400 / 404) | **No override needed** — see below |
| HTTP 200 + sentinel text in body | substring on `response.text` |
| HTTP 200 + redirect to a different URL | check `response.url` |
| HTTP 200 + empty results table | row-count on the table xpath |

**HTTP 4xx — no override needed.** The speculation driver auto-converts 4xx
responses into miss outcomes via `SpeculationHTTPFailure`
(`fails_successfully` is only called for 200–299). Don't write a
`fails_successfully` override for 4xx responses; the gap counter advances
and no error row is emitted. Maryland is the canonical exemplar.

**Sentinel text in body:**
```python
def fails_successfully(self, response: Response) -> bool:
    return "case not found" not in response.text.lower()
```

**Redirect to a different URL** (Massachusetts exemplar — invalid IDs `302`
to the search landing):
```python
def fails_successfully(self, response: Response) -> bool:
    """Soft-404 detection: invalid IDs redirect to /docket landing."""
    url = response.url or ""
    if "/docket/" not in url:
        return "/calendar/" in url  # other valid endpoints pass
    return True
```

**Empty results table:**
```python
def fails_successfully(self, response: Response) -> bool:
    page = LxmlPageElement.from_response(response)
    rows = page.query_xpath("//table[@id='results']//tr", "rows", min_count=0)
    return len(rows) > 1  # > 1 because header row counts as one
```

### Document downloads

For downloadable documents (opinions, briefs, etc.):
```python
yield Request(
    archive=True,
    request=HTTPRequestParams(method=HttpMethod.GET, url=pdf_url),
    continuation=self.handle_document_download,
    expected_type="pdf",
    accumulated_data={...},
)
```

**Memory caveat for Playwright scrapers.** Archive requests stream
chunk-by-chunk to disk under the default streaming archive handler — sync,
async, and persistent drivers all set `ArchiveResponse.content = b""` and
never buffer the body. Playwright is the exception: when an archive Request
has no `via` (a bare URL — the pattern shown above), the body is fetched
through Playwright's `APIRequestContext` which has no streaming API, so the
whole file is materialized in memory before being re-chunked to the handler.
Via-driven archive downloads (a Playwright `download` event triggered by a
click on an anchor in the parent page) *do* stream, because Playwright
itself writes the file to disk and kent re-reads it in 64KB chunks. For
potentially large files (hundreds of MB+) on a Playwright scraper, prefer
triggering the download via a `find_links()` / `find_form()`-derived anchor
rather than constructing a bare archive Request from a URL string.

### Deduplication

Use `deduplication_key` on Requests to avoid visiting the same case twice
when overlapping searches produce duplicate results:

```python
yield Request(
    request=HTTPRequestParams(method=HttpMethod.GET, url=case_url),
    continuation=self.parse_case,
    deduplication_key=docket_id,  # same docket_id won't be fetched twice
)
```

For pagination requests that must always execute, skip dedup:

```python
from kent.data_types import SkipDeduplicationCheck

yield Request(
    request=HTTPRequestParams(method=HttpMethod.GET, url=next_page_url),
    continuation=self.parse_results,
    deduplication_key=SkipDeduplicationCheck(),
)
```

### Pagination

**HTML next-link pagination**: follow "Next" links with
`page.find_links("//a[contains(text(), 'Next')]", ...)`.

**API offset pagination**: track `page` in `accumulated_data`, increment,
and yield a new Request until `current_page >= total_pages`.

**Newest-sorted listing walk**: (no date filter) walk pages in
sortOrder=Newest, stop when the oldest in-window item on the page is older
than `date_gte`.

**Date-range splitting**: some APIs cap results (e.g. 10,000). If a search
returns the maximum, split the date range in half and re-search each half.

All pagination requests should use
`deduplication_key=SkipDeduplicationCheck()`.

### Driver requirements

If Phase 4 determines Playwright is needed, add to the class:

```python
from kent.data_types import DriverRequirement

driver_requirements: ClassVar[list[DriverRequirement]] = [
    DriverRequirement.JS_EVAL,
    DriverRequirement.FF_ALIKE,
]
```

For the canonical and current list of values, see
[kent-api-reference.md](kent-api-reference.md). Existing scrapers use
site-specific values like `H11_HEADER_FIXES` and `FOLLOW_REDIRECTS` (Alaska)
in addition to the common `JS_EVAL`, `FF_ALIKE`, `CHROME_ALIKE`,
`HCAP_HANDLER`, `RCAP_HANDLER`.

Steps that need to wait for JS rendering should use `@step(await_list=[...])`:

```python
from kent.data_types import WaitForLoadState, WaitForSelector

@step(await_list=[
    WaitForLoadState("networkidle"),
    WaitForSelector("table.results"),
])
def parse_results(self, page, accumulated_data):
    ...
```

---

## Checklist Before Finishing

- [ ] Phase 1.5: identified canonical sibling exemplars from the table
- [ ] DESIGN.md documents all findings from Phases 1–4
- [ ] Court mapping table is complete with CourtListener IDs
- [ ] models.py has all ScrapedData models with typed fields
- [ ] Models include at least `*Docket`, `*DocketEntry`, and `*Document` (for files, if there are any)
- [ ] Future-calendar / scheduled-hearing items are modelled as `DocketEntry`, *not* as a separate hearing type
- [ ] scraper.py has proper class metadata (court_ids, data_types, status, version, rate_limits)
- [ ] `driver_requirements` set if Playwright needed (scraper-wide; binary HTTP-vs-Playwright)
- [ ] Entry points cover all courts (one per court if speculative; one per (court, type) prefix for multi-prefix sites)
- [ ] Entry points cover oral arguments if the site has a calendar
- [ ] Step functions parse every tab/section discovered in Phase 3 (or one parse step if single-page portal)
- [ ] Pagination handled with `SkipDeduplicationCheck()` on next-page requests
- [ ] Custom `deduplication_key` set where overlapping searches may yield duplicates
- [ ] Document downloads use `archive=True`
- [ ] `accumulated_data` values are JSON-serializable
- [ ] Email notification capability is documented (not necessarily implemented)
- [ ] Bot protection / CSRF fields are handled in form submissions
- [ ] `__init__.py` exists in **both** the scraper's directory **and** the parent state directory (create one if this is the first scraper for that state)

---

## Patterns Library (Appendix)

Symptom-triggered patterns for site shapes that don't fit the default flow.
Each entry leads with a one-line `Symptom:` so you can skim/grep.

### Invisible hCaptcha (token-as-header)

**Symptom:** SPA fetches a JWT-shaped token (prefixed `P1_eyJ…`) from
`api.hcaptcha.com/getcaptcha/{sitekey}` and attaches it to a custom request
header (commonly `captchatoken:`) on every gated fetch. There is no visible
challenge widget, no `div.h-captcha` in the DOM, no checkbox to click.

**Diagnosis:** Look for `api.hcaptcha.com/getcaptcha/{sitekey}` calls in the
network panel with no visible challenge surface. Direct curl to the gated
endpoint returns `{"error":"Captcha validation failed."}` or similar.

**Recommendation: punt to listing-only v1.** kent's `HCAP_HANDLER` driver
requirement targets *visible* hCaptcha widgets only — `HCaptchaHandler` in
`kent/driver/interstitials.py` looks for `div.h-captcha` and clicks it.
There is no built-in driver requirement for invisible / execute-mode
hCaptcha as of 2026-05.

A `nonnavigating=True` request alone won't fix this either: the Playwright
driver dispatches every non-archive request through `page.goto(url)`
regardless of `nonnavigating`. When the gate is purely per-fetch-header
(`captchatoken` JWT, no cookie fallback), navigating the same tab to a real
case page first and then `page.goto(detail_api_url)` still returns
`Captcha validation failed`. The token exists for the duration of one SPA
fetch and isn't reachable to a follow-up navigation.

**Recommended path:** ship listing-only v1 with `status=IN_DEVELOPMENT`,
list the captcha gap in DESIGN.md, and move on. A real fix requires a kent
affordance for "execute this fetch from inside the page's JS context" (a
`ViaPageEvaluate` request kind, or `PageElement.evaluate()` exposed to
steps) that does not exist today. Don't sink hours into a per-scraper
workaround.

---

### Episerver SSR JSON via `?expand=*&currentPageUrl=...`

**Symptom:** The site is Episerver / Optimizely (look for
`/api/episerver/v2.0/content/...` calls in the network panel). The visible
`/api/Foo/Bar` endpoints have buggy or unreliable pagination, sort, or
filter behavior — sending `page=`, `pageSize=`, or date params seems to be
silently ignored.

**Pattern:** The page URL itself returns a JSON object when called with
`?expand=*&currentPageUrl={routeSegment}` and `Accept: application/json`.
The server returns the entire Episerver page object including a data field
(e.g. `caseSearchResults`) with the actual records. No captcha, no session.

**Why it matters:** This SSR variant is often more capable than the visible
APIs — it'll honour pagination/sort/filter params that the dedicated API
ignores. On Michigan,
`/api/CaseSearch/AdvancedSearchCaseDetails` silently ignores `page=`,
`pageSize=`, and every date param tried, while the SSR variant honours
`page=1..N` and `pageSize` up to 100.

**Recommendation:** When you detect Episerver, **probe both endpoints**
before deciding the site can't paginate server-side. Often the SSR variant
is what the SPA actually consumes. See Michigan
([courts_michigan_gov](../../../juriscraper/sd/state/michigan/courts_michigan_gov/scraper.py))
as the canonical exemplar.

---

### `resultType=` parallel result containers

**Symptom:** A single listing endpoint returns multiple result containers
(e.g. `caseDetailResults`, `opinionResults`, `orderResults`) in one JSON
response. A query parameter like `resultType=cases | opinions | orders`
selects which container is populated.

**Why it matters:** Sites that publish opinions/orders as separate document
indexes often expose them via this parameter on the same listing endpoint
that serves cases. These parallel indexes are usually the right entry
points for opinion/order scrapers, even when individual cases also expose
nested document arrays.

**Recommendation:** Test `resultType=cases`, `resultType=opinions`,
`resultType=orders` (or the site's equivalents) on the listing endpoint and
inspect each response. If the site exposes parallel indexes, this is
typically the right entry point for an opinions/orders scraper rather than
walking nested document arrays inside cases.