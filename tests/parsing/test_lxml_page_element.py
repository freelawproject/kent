"""Tests for LxmlPageElement implementation.

Tests delegation to CheckedHtmlElement, observer integration, form and link handling.
"""

import pytest
from lxml import html

from jkent.common.checked_html import CheckedHtmlElement
from jkent.common.exceptions import (
    HTMLStructuralAssumptionException,
)
from jkent.common.lxml_page_element import (
    LxmlPageElement,
)
from jkent.common.page_element import ViaLink
from jkent.common.selector_observer import (
    SelectorObserver,
)


@pytest.fixture
def simple_page():
    """Simple HTML page for testing."""
    html_content = """
    <html>
    <body>
        <div id="main">
            <h1>Test Page</h1>
            <table>
                <tr class="row"><td>Cell 1</td><td>Cell 2</td></tr>
                <tr class="row"><td>Cell 3</td><td>Cell 4</td></tr>
            </table>
        </div>
    </body>
    </html>
    """
    doc = html.fromstring(html_content)
    checked = CheckedHtmlElement(doc, "https://example.com/page")
    return LxmlPageElement(checked, "https://example.com/page")


@pytest.fixture
def form_page():
    """HTML page with a form for testing."""
    html_content = """
    <html>
    <body>
        <form id="search" action="/search" method="POST">
            <input type="text" name="query" value="" />
            <input type="hidden" name="token" value="abc123" />
            <select name="category">
                <option value="all">All</option>
                <option value="news" selected>News</option>
                <option value="blog">Blog</option>
            </select>
            <textarea name="description">Default text</textarea>
            <button type="submit">Search</button>
        </form>
    </body>
    </html>
    """
    doc = html.fromstring(html_content)
    checked = CheckedHtmlElement(doc, "https://example.com/")
    return LxmlPageElement(checked, "https://example.com/")


@pytest.fixture
def links_page():
    """HTML page with links for testing."""
    html_content = """
    <html>
    <body>
        <nav>
            <a href="/page1" class="nav-link">Page 1</a>
            <a href="/page2" class="nav-link">Page 2</a>
            <a href="https://external.com/page3">External</a>
        </nav>
        <div>
            <a>No href</a>
        </div>
    </body>
    </html>
    """
    doc = html.fromstring(html_content)
    checked = CheckedHtmlElement(doc, "https://example.com/")
    return LxmlPageElement(checked, "https://example.com/")


def test_query_xpath_delegation(simple_page):
    """query_xpath should delegate to CheckedHtmlElement."""
    rows = simple_page.query_xpath("//tr[@class='row']", "rows")

    assert len(rows) == 2
    # Results should be wrapped in LxmlPageElement
    assert all(isinstance(row, LxmlPageElement) for row in rows)


def test_query_xpath_returns_lxml_page_elements(simple_page):
    """query_xpath should return LxmlPageElement instances."""
    rows = simple_page.query_xpath("//tr", "rows", min_count=2)

    # Each result should be LxmlPageElement
    for row in rows:
        assert isinstance(row, LxmlPageElement)
        # Should be able to query nested elements
        cells = row.query_xpath(".//td", "cells")
        assert len(cells) == 2


def test_query_xpath_strings(simple_page):
    """query_xpath_strings should return string values."""
    cell_texts = simple_page.query_xpath_strings(
        "//td/text()", "cell texts", min_count=4, max_count=4
    )

    assert len(cell_texts) == 4
    assert all(isinstance(text, str) for text in cell_texts)
    assert "Cell 1" in cell_texts


def test_query_css_delegation(simple_page):
    """query_css should delegate to CheckedHtmlElement."""
    rows = simple_page.query_css("tr.row", "rows")

    assert len(rows) == 2
    assert all(isinstance(row, LxmlPageElement) for row in rows)


def test_text_content(simple_page):
    """text_content should return element text."""
    h1 = simple_page.query_xpath("//h1", "heading", min_count=1, max_count=1)[
        0
    ]

    assert h1.text_content() == "Test Page"


def test_get_attribute(links_page):
    """get_attribute should return attribute value."""
    link = links_page.query_xpath("//a[@class='nav-link']", "nav links")[0]

    assert link.get_attribute("href") == "/page1"
    assert link.get_attribute("class") == "nav-link"
    assert link.get_attribute("nonexistent") is None


def test_inner_html(simple_page):
    """inner_html should return inner HTML content."""
    div = simple_page.query_xpath("//div[@id='main']", "main div")[0]

    inner = div.inner_html()

    assert "<h1>Test Page</h1>" in inner
    assert "<table>" in inner


def test_tag_name(simple_page):
    """tag_name should return lowercase tag name."""
    h1 = simple_page.query_xpath("//h1", "heading")[0]
    table = simple_page.query_xpath("//table", "table")[0]

    assert h1.tag_name() == "h1"
    assert table.tag_name() == "table"


def test_child_elements_inherit_observer(simple_page):
    """Child elements should inherit parent's observer."""
    observer = SelectorObserver()
    doc = html.fromstring("<div><p>Test</p></div>")
    checked = CheckedHtmlElement(doc, "https://example.com/")
    page = LxmlPageElement(checked, "https://example.com/", observer)

    # Query for child element
    paragraphs = page.query_xpath("//p", "paragraphs")

    # Child should have the same observer
    assert paragraphs[0]._observer is observer


def test_find_form_by_xpath(form_page):
    """find_form should find form by XPath selector."""
    form = form_page.find_form("//form[@id='search']", "search form")

    assert form.action == "https://example.com/search"
    assert form.method == "POST"
    assert form.selector == "//form[@id='search']"
    assert len(form.fields) == 4  # query, token, category, description


def test_find_form_by_css(form_page):
    """find_form should find form by CSS selector."""
    form = form_page.find_form("form#search", "search form")

    assert form.action == "https://example.com/search"
    assert form.method == "POST"


def test_form_fields_extraction(form_page):
    """find_form should extract all form fields correctly."""
    form = form_page.find_form("//form", "form")

    # Check text input
    query_field = form.get_field("query")
    assert query_field is not None
    assert query_field.field_type == "text"
    assert query_field.value == ""

    # Check hidden input
    token_field = form.get_field("token")
    assert token_field is not None
    assert token_field.field_type == "hidden"
    assert token_field.value == "abc123"

    # Check select
    category_field = form.get_field("category")
    assert category_field is not None
    assert category_field.field_type == "select"
    assert category_field.value == "news"  # Selected option
    assert category_field.options == ["all", "news", "blog"]

    # Check textarea
    desc_field = form.get_field("description")
    assert desc_field is not None
    assert desc_field.field_type == "textarea"
    assert desc_field.value == "Default text"


def test_form_action_resolution(form_page):
    """find_form should resolve relative action URLs."""
    # Absolute URL in action
    html_content = (
        '<form action="https://other.com/submit"><input name="test"/></form>'
    )
    doc = html.fromstring(html_content)
    checked = CheckedHtmlElement(doc, "https://example.com/")
    page = LxmlPageElement(checked, "https://example.com/")

    form = page.find_form("//form", "form")
    assert form.action == "https://other.com/submit"


def test_form_no_action_uses_base_url(form_page):
    """find_form should use base URL when form has no action."""
    html_content = '<form><input name="test"/></form>'
    doc = html.fromstring(html_content)
    checked = CheckedHtmlElement(doc, "https://example.com/page")
    page = LxmlPageElement(checked, "https://example.com/page")

    form = page.find_form("//form", "form")
    assert form.action == "https://example.com/page"


def test_find_links_by_xpath(links_page):
    """find_links should find links by XPath selector."""
    links = links_page.find_links("//a[@class='nav-link']", "nav links")

    assert len(links) == 2
    assert links[0].url == "https://example.com/page1"
    assert links[0].text == "Page 1"
    assert links[1].url == "https://example.com/page2"
    assert links[1].text == "Page 2"


def test_find_links_by_css(links_page):
    """find_links should find links by CSS selector."""
    links = links_page.find_links("a.nav-link", "nav links")

    assert len(links) == 2
    assert links[0].url == "https://example.com/page1"


def test_find_links_resolves_urls(links_page):
    """find_links should resolve relative URLs."""
    links = links_page.find_links("//a[@class='nav-link']", "nav links")

    # Relative URLs should be resolved
    assert links[0].url == "https://example.com/page1"
    assert links[1].url == "https://example.com/page2"


def test_find_links_skips_links_without_href(links_page):
    """find_links should skip <a> elements without href."""
    all_links = links_page.links()

    # Should find 3 links (not the one without href)
    assert len(all_links) == 3


def test_links_returns_all_links(links_page):
    """links() should return all <a> elements with href."""
    all_links = links_page.links()

    assert len(all_links) == 3
    # Should include both relative and absolute URLs
    urls = [link.url for link in all_links]
    assert "https://example.com/page1" in urls
    assert "https://external.com/page3" in urls


def test_link_follow_creates_navigating_request(links_page):
    """Link.follow() should create a Request with ViaLink."""
    links = links_page.find_links("//a[@class='nav-link']", "nav links")
    link = links[0]

    request = link.follow()

    assert request.request.url == "https://example.com/page1"
    assert request.via is not None
    assert isinstance(request.via, ViaLink)
    assert "nav-link" in request.via.selector


def test_find_form_raises_on_no_match(simple_page):
    """find_form should raise if no form matches."""
    with pytest.raises(HTMLStructuralAssumptionException):
        simple_page.find_form("//form[@id='nonexistent']", "missing form")


def _page_from_html(html_content: str, base_url: str = "https://example.com/"):
    doc = html.fromstring(html_content)
    checked = CheckedHtmlElement(doc, base_url)
    return LxmlPageElement(checked, base_url)


def test_unchecked_checkbox_with_value_is_omitted():
    """Unchecked checkboxes must not appear in the submitted form data."""
    page = _page_from_html(
        '<form id="f" action="/x" method="post">'
        '<input name="cb" type="checkbox" value="true" />'
        "</form>"
    )
    form = page.find_form("//form[@id='f']", "f")

    assert form.get_field("cb") is None
    request = form.submit()
    assert "cb" not in request.request.data


def test_checked_checkbox_with_value_is_submitted():
    """Checked checkboxes submit their explicit value attribute."""
    page = _page_from_html(
        '<form id="f" action="/x" method="post">'
        '<input name="cb" type="checkbox" value="yes" checked />'
        "</form>"
    )
    form = page.find_form("//form[@id='f']", "f")

    field = form.get_field("cb")
    assert field is not None
    assert field.value == "yes"
    request = form.submit()
    data = request.request.data
    assert isinstance(data, dict)
    assert data["cb"] == "yes"


def test_checked_checkbox_without_value_defaults_to_on():
    """Checked checkboxes without a value attribute submit as 'on'."""
    page = _page_from_html(
        '<form id="f" action="/x" method="post">'
        '<input name="cb" type="checkbox" checked />'
        "</form>"
    )
    form = page.find_form("//form[@id='f']", "f")

    field = form.get_field("cb")
    assert field is not None
    assert field.value == "on"
    request = form.submit()
    data = request.request.data
    assert isinstance(data, dict)
    assert data["cb"] == "on"


def test_unchecked_checkbox_without_value_is_omitted():
    """Unchecked checkboxes without a value attribute are still omitted."""
    page = _page_from_html(
        '<form id="f" action="/x" method="post">'
        '<input name="cb" type="checkbox" />'
        "</form>"
    )
    form = page.find_form("//form[@id='f']", "f")

    assert form.get_field("cb") is None
    request = form.submit()
    assert "cb" not in request.request.data


def test_mixed_checkboxes_only_checked_submitted():
    """When two checkboxes share a name, only the checked one is submitted."""
    page = _page_from_html(
        '<form id="f" action="/x" method="post">'
        '<input name="cb" type="checkbox" value="a" />'
        '<input name="cb" type="checkbox" value="b" checked />'
        "</form>"
    )
    form = page.find_form("//form[@id='f']", "f")

    cb_fields = [f for f in form.fields if f.name == "cb"]
    assert len(cb_fields) == 1
    assert cb_fields[0].value == "b"
    request = form.submit()
    data = request.request.data
    assert isinstance(data, dict)
    assert data["cb"] == "b"


def test_query_count_validation(simple_page):
    """Query methods should validate count constraints."""
    # Too few
    with pytest.raises(HTMLStructuralAssumptionException):
        simple_page.query_xpath("//tr", "rows", min_count=10)

    # Too many
    with pytest.raises(HTMLStructuralAssumptionException):
        simple_page.query_xpath("//tr", "rows", min_count=1, max_count=1)


def test_link_selector_includes_position(links_page):
    """find_links should create positional selectors for each link."""
    links = links_page.find_links("//a[@class='nav-link']", "nav links")

    # Each link should have a unique positional selector
    assert "[1]" in links[0].selector
    assert "[2]" in links[1].selector
