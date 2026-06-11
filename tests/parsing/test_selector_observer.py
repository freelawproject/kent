"""Tests for SelectorObserver.

Tests recording logic, deduplication, output formats, and absolute selector composition.
"""

import pytest
from lxml import html

from jkent.common.checked_html import CheckedHtmlElement
from jkent.common.selector_observer import (
    SelectorObserver,
    SelectorQuery,
    get_active_observer,
)


@pytest.fixture
def simple_html():
    """Simple HTML document for testing."""
    return """
    <html>
    <body>
        <div id="main">
            <table>
                <tr class="row"><td>Cell 1</td><td>Cell 2</td></tr>
                <tr class="row"><td>Cell 3</td><td>Cell 4</td></tr>
                <tr class="row"><td>Cell 5</td><td>Cell 6</td></tr>
            </table>
        </div>
    </body>
    </html>
    """


def test_observer_starts_empty():
    """SelectorObserver should start with no recorded queries."""
    observer = SelectorObserver()
    assert observer.queries == []


def test_context_manager_sets_active_observer():
    """Entering the context makes the observer the active one."""
    assert get_active_observer() is None

    with SelectorObserver() as observer:
        assert get_active_observer() is observer

    assert get_active_observer() is None


def test_nested_context_managers_restore_previous():
    """Nested observers take over, and the outer is restored on exit."""
    with SelectorObserver() as outer:
        assert get_active_observer() is outer

        with SelectorObserver() as inner:
            assert get_active_observer() is inner

        assert get_active_observer() is outer

    assert get_active_observer() is None


def test_checked_html_element_reports_to_active_observer(simple_html):
    """CheckedHtmlElement records queries to the active observer."""
    tree = CheckedHtmlElement(
        html.fromstring(simple_html), "http://example.com"
    )

    with SelectorObserver() as observer:
        rows = tree.checked_xpath("//tr[@class='row']", "rows")
        assert len(rows) == 3

    assert len(observer.queries) == 1
    assert observer.queries[0].selector == "//tr[@class='row']"
    assert observer.queries[0].match_count == 3


def test_no_active_observer_outside_context(simple_html):
    """CheckedHtmlElement still works when no observer is active."""
    assert get_active_observer() is None

    tree = CheckedHtmlElement(
        html.fromstring(simple_html), "http://example.com"
    )
    rows = tree.checked_xpath("//tr[@class='row']", "rows")
    assert len(rows) == 3

    assert get_active_observer() is None


def test_record_simple_query(simple_html):
    """Observer should record a simple query."""
    doc = html.fromstring(simple_html)
    observer = SelectorObserver()

    results = doc.xpath("//tr[@class='row']")
    observer.record_query(
        selector="//tr[@class='row']",
        selector_type="xpath",
        description="table rows",
        results=results,
        expected_min=1,
        expected_max=None,
    )

    assert len(observer.queries) == 1
    query = observer.queries[0]
    assert query.selector == "//tr[@class='row']"
    assert query.selector_type == "xpath"
    assert query.description == "table rows"
    assert query.match_count == 3
    assert query.expected_min == 1
    assert query.expected_max is None


def test_record_nested_queries(simple_html):
    """Observer should record nested queries with parent-child relationships."""
    doc = html.fromstring(simple_html)
    observer = SelectorObserver()

    # First query: find rows
    rows = doc.xpath("//tr[@class='row']")
    observer.record_query(
        selector="//tr[@class='row']",
        selector_type="xpath",
        description="table rows",
        results=rows,
        expected_min=1,
        expected_max=None,
    )

    # Second query: find cells within first row
    cells = rows[0].xpath(".//td")
    observer.record_query(
        selector=".//td",
        selector_type="xpath",
        description="cells",
        results=cells,
        expected_min=1,
        expected_max=None,
        parent_element=rows[0],
    )

    # Should have one top-level query
    assert len(observer.queries) == 1

    # Top-level query should have one child
    parent_query = observer.queries[0]
    assert len(parent_query.children) == 1

    # Child query should reference parent
    child_query = parent_query.children[0]
    assert child_query.selector == ".//td"
    assert child_query.parent == parent_query
    assert child_query.parent_element_id == parent_query.element_id


def test_deduplication_same_selector(simple_html):
    """Observer should deduplicate repeated queries with the same parent."""
    doc = html.fromstring(simple_html)
    observer = SelectorObserver()

    # Find rows
    rows = doc.xpath("//tr[@class='row']")
    observer.record_query(
        selector="//tr[@class='row']",
        selector_type="xpath",
        description="table rows",
        results=rows,
        expected_min=1,
        expected_max=None,
    )

    # Query cells for each row (same selector, same parent type)
    for row in rows:
        cells = row.xpath(".//td")
        observer.record_query(
            selector=".//td",
            selector_type="xpath",
            description="cells",
            results=cells,
            expected_min=1,
            expected_max=None,
            parent_element=row,
        )

    # Should have one top-level query
    assert len(observer.queries) == 1

    # Should have ONE child query (deduplicated)
    parent_query = observer.queries[0]
    assert len(parent_query.children) == 1

    # Child query should have aggregated match count (2 cells * 3 rows = 6)
    child_query = parent_query.children[0]
    assert child_query.match_count == 6


def test_sample_extraction(simple_html):
    """Observer should extract sample content from results."""
    doc = html.fromstring(simple_html)
    observer = SelectorObserver(max_samples=2)

    rows = doc.xpath("//tr[@class='row']")
    observer.record_query(
        selector="//tr[@class='row']",
        selector_type="xpath",
        description="table rows",
        results=rows,
        expected_min=1,
        expected_max=None,
    )

    query = observer.queries[0]
    assert len(query.sample_elements) == 2  # max_samples
    # First row contains "Cell 1Cell 2" (normalized whitespace)
    assert "Cell 1" in query.sample_elements[0]
    assert "Cell 2" in query.sample_elements[0]


def test_simple_tree_output(simple_html):
    """Observer should generate human-readable tree output."""
    doc = html.fromstring(simple_html)
    observer = SelectorObserver()

    rows = doc.xpath("//tr[@class='row']")
    observer.record_query(
        selector="//tr[@class='row']",
        selector_type="xpath",
        description="table rows",
        results=rows,
        expected_min=1,
        expected_max=None,
    )

    cells = rows[0].xpath(".//td")
    observer.record_query(
        selector=".//td",
        selector_type="xpath",
        description="cells",
        results=cells,
        expected_min=1,
        expected_max=None,
        parent_element=rows[0],
    )

    tree = observer.simple_tree()

    # Should contain parent query
    assert "//tr[@class='row']" in tree
    assert "table rows" in tree
    assert "✓" in tree  # Success indicator

    # Should contain child query (indented)
    assert ".//td" in tree
    assert "cells" in tree


def test_simple_tree_failure_indicator(simple_html):
    """Observer should show ✗ for failed queries."""
    doc = html.fromstring(simple_html)
    observer = SelectorObserver()

    # Query that finds nothing
    results = doc.xpath("//nonexistent")
    observer.record_query(
        selector="//nonexistent",
        selector_type="xpath",
        description="missing elements",
        results=results,
        expected_min=1,
        expected_max=None,
    )

    tree = observer.simple_tree()

    assert "✗" in tree
    assert "0 matches" in tree
    assert "expected 1+" in tree


def test_json_output(simple_html):
    """Observer should generate JSON output."""
    doc = html.fromstring(simple_html)
    observer = SelectorObserver()

    rows = doc.xpath("//tr[@class='row']")
    observer.record_query(
        selector="//tr[@class='row']",
        selector_type="xpath",
        description="table rows",
        results=rows,
        expected_min=1,
        expected_max=None,
    )

    json_output = observer.json()

    assert isinstance(json_output, list)
    assert len(json_output) == 1

    query_dict = json_output[0]
    assert query_dict["selector"] == "//tr[@class='row']"
    assert query_dict["selector_type"] == "xpath"
    assert query_dict["description"] == "table rows"
    assert query_dict["match_count"] == 3
    assert query_dict["element_id"] is not None


def test_compose_absolute_selector_simple():
    """compose_absolute_selector should return selector for root query."""
    query = SelectorQuery(
        selector="//table",
        selector_type="xpath",
        description="table",
        match_count=1,
        expected_min=1,
        expected_max=None,
        parent=None,
    )

    observer = SelectorObserver()
    absolute = observer.compose_absolute_selector(query)

    assert absolute == "//table"


def test_compose_absolute_selector_nested_xpath():
    """compose_absolute_selector should compose nested XPath selectors."""
    parent = SelectorQuery(
        selector="//table",
        selector_type="xpath",
        description="table",
        match_count=1,
        expected_min=1,
        expected_max=None,
        parent=None,
    )

    child = SelectorQuery(
        selector=".//tr",
        selector_type="xpath",
        description="rows",
        match_count=3,
        expected_min=1,
        expected_max=None,
        parent=parent,
    )

    observer = SelectorObserver()
    absolute = observer.compose_absolute_selector(child)

    # Should strip "./" and compose
    assert absolute == "//table//tr"


def test_compose_absolute_selector_mixed_types():
    """compose_absolute_selector should return None for mixed selector types."""
    parent = SelectorQuery(
        selector="table",
        selector_type="css",
        description="table",
        match_count=1,
        expected_min=1,
        expected_max=None,
        parent=None,
    )

    child = SelectorQuery(
        selector=".//tr",
        selector_type="xpath",
        description="rows",
        match_count=3,
        expected_min=1,
        expected_max=None,
        parent=parent,
    )

    observer = SelectorObserver()
    absolute = observer.compose_absolute_selector(child)

    # Mixed types - cannot compose
    assert absolute is None


def test_compose_absolute_selector_css():
    """compose_absolute_selector should compose CSS selectors with space."""
    parent = SelectorQuery(
        selector="div.container",
        selector_type="css",
        description="container",
        match_count=1,
        expected_min=1,
        expected_max=None,
        parent=None,
    )

    child = SelectorQuery(
        selector="table",
        selector_type="css",
        description="table",
        match_count=1,
        expected_min=1,
        expected_max=None,
        parent=parent,
    )

    observer = SelectorObserver()
    absolute = observer.compose_absolute_selector(child)

    assert absolute == "div.container table"


def test_compose_absolute_selector_three_levels():
    """compose_absolute_selector should handle deep nesting."""
    level1 = SelectorQuery(
        selector="//div",
        selector_type="xpath",
        description="div",
        match_count=1,
        expected_min=1,
        expected_max=None,
        parent=None,
    )

    level2 = SelectorQuery(
        selector=".//table",
        selector_type="xpath",
        description="table",
        match_count=1,
        expected_min=1,
        expected_max=None,
        parent=level1,
    )

    level3 = SelectorQuery(
        selector=".//tr",
        selector_type="xpath",
        description="rows",
        match_count=3,
        expected_min=1,
        expected_max=None,
        parent=level2,
    )

    observer = SelectorObserver()
    absolute = observer.compose_absolute_selector(level3)

    assert absolute == "//div//table//tr"


def test_max_samples_limit():
    """Observer should respect max_samples limit."""
    doc = html.fromstring(
        "<div><p>1</p><p>2</p><p>3</p><p>4</p><p>5</p></div>"
    )
    observer = SelectorObserver(max_samples=2)

    results = doc.xpath("//p")
    observer.record_query(
        selector="//p",
        selector_type="xpath",
        description="paragraphs",
        results=results,
        expected_min=1,
        expected_max=None,
    )

    query = observer.queries[0]
    assert len(query.sample_elements) == 2


def test_max_sample_length():
    """Observer should truncate long sample text."""
    long_text = "A" * 200
    doc = html.fromstring(f"<div><p>{long_text}</p></div>")
    observer = SelectorObserver(max_sample_length=50)

    results = doc.xpath("//p")
    observer.record_query(
        selector="//p",
        selector_type="xpath",
        description="paragraph",
        results=results,
        expected_min=1,
        expected_max=None,
    )

    query = observer.queries[0]
    assert len(query.sample_elements[0]) <= 53  # 50 + "..."
    assert query.sample_elements[0].endswith("...")
