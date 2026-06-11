"""Checked HTML element wrapper for safe XPath/CSS querying.

This module provides CheckedHtmlElement, a wrapper around lxml.html.HtmlElement
that validates selector results against expected counts. This helps catch
structural assumption violations early.

Step 8 introduces this pattern for better error handling.
"""

from __future__ import annotations

from typing import overload

from lxml.html import HtmlElement

from jkent.common.exceptions import (
    HTMLStructuralAssumptionException,
)
from jkent.common.selector_observer import (
    get_active_observer,
)
from jkent.contracts import ensure, require


class CheckedHtmlElement:
    """Wrapper around HtmlElement with validated selectors.

    This class wraps an lxml HtmlElement and provides checked_xpath() and
    checked_css() methods that validate the number of results against expected
    min/max counts. If the actual count doesn't match expectations, it raises
    HTMLStructuralAssumptionException with clear error context.

    This helps catch website structure changes early and provides clear error
    messages for debugging.
    """

    def __init__(self, element: HtmlElement, request_url: str = "") -> None:
        """Initialize the checked element wrapper.

        Args:
            element: The lxml HtmlElement to wrap.
            request_url: Optional URL for error context.
        """
        self._element = element
        self._request_url = request_url

    @overload
    def checked_xpath(
        self,
        xpath: str,
        description: str,
        min_count: int = 1,
        max_count: int | None = None,
        *,
        type: type[str],
    ) -> list[str]: ...

    @overload
    def checked_xpath(
        self,
        xpath: str,
        description: str,
        min_count: int = 1,
        max_count: int | None = None,
    ) -> list[CheckedHtmlElement]: ...

    @require(
        lambda min_count, max_count: min_count >= 0
        and (max_count is None or max_count >= min_count),
        "expected-count bounds form a valid (possibly open) interval",
    )
    @ensure(
        lambda result, min_count, max_count: min_count <= len(result)
        and (max_count is None or len(result) <= max_count),
        "a returned result list always satisfies the caller's bounds — "
        "out-of-bounds counts raise instead",
    )
    # pyre-ignore[43]: contracts decorate only the implementation, not
    # the @overload stubs — they're identity functions to type checkers.
    def checked_xpath(
        self,
        xpath: str,
        description: str,
        min_count: int = 1,
        max_count: int | None = None,
        *,
        type: type[str] | None = None,
    ) -> list[CheckedHtmlElement] | list[str]:
        """Execute XPath query with count validation.

        Args:
            xpath: XPath expression to execute.
            description: Human-readable description of what's being selected.
            min_count: Minimum number of elements expected (default: 1).
            max_count: Maximum number of elements expected (None = unlimited).
            type: Pass `str` to return only string results (text/attributes).
                If omitted, returns only CheckedHtmlElements (filtering out
                any string results).

        Returns:
            List of matching results filtered by type. By default returns
            CheckedHtmlElements; pass type=str for string results.

        Raises:
            HTMLStructuralAssumptionException: If count doesn't match expectations.

        Example::

            tree = CheckedHtmlElement(lxml.html.fromstring(html))
            # Get elements (default)
            cases = tree.checked_xpath("//tr[@class='case']", "cases")
            # Get text/attributes
            hrefs = tree.checked_xpath("//a/@href", "links", type=str)
        """
        results = self._element.xpath(xpath)

        # Report to active observer if present
        observer = get_active_observer()
        if observer is not None:
            observer.record_query(
                selector=xpath,
                selector_type="xpath",
                description=description,
                results=results,
                expected_min=min_count,
                expected_max=max_count,
                parent_element=self._element,
            )

        if type is str:
            # Return only string results
            filtered: list[str] = [r for r in results if isinstance(r, str)]
            actual_count = len(filtered)
            if actual_count < min_count or (
                max_count is not None and actual_count > max_count
            ):
                raise HTMLStructuralAssumptionException(
                    selector=xpath,
                    selector_type="xpath",
                    description=description,
                    expected_min=min_count,
                    expected_max=max_count,
                    actual_count=actual_count,
                    request_url=self._request_url,
                    is_element_query=False,
                )
            return filtered
        else:
            # Return only element results, wrapped in CheckedHtmlElement
            wrapped: list[CheckedHtmlElement] = [
                CheckedHtmlElement(r, self._request_url)
                for r in results
                if isinstance(r, HtmlElement)
            ]
            actual_count = len(wrapped)
            if actual_count < min_count or (
                max_count is not None and actual_count > max_count
            ):
                raise HTMLStructuralAssumptionException(
                    selector=xpath,
                    selector_type="xpath",
                    description=description,
                    expected_min=min_count,
                    expected_max=max_count,
                    actual_count=actual_count,
                    request_url=self._request_url,
                )
            return wrapped

    @require(
        lambda min_count, max_count: min_count >= 0
        and (max_count is None or max_count >= min_count),
        "expected-count bounds form a valid (possibly open) interval",
    )
    @ensure(
        lambda result, min_count, max_count: min_count <= len(result)
        and (max_count is None or len(result) <= max_count),
        "a returned result list always satisfies the caller's bounds — "
        "out-of-bounds counts raise instead",
    )
    def checked_css(
        self,
        selector: str,
        description: str,
        min_count: int = 1,
        max_count: int | None = None,
    ) -> list[CheckedHtmlElement]:
        """Execute CSS selector query with count validation.

        Args:
            selector: CSS selector expression.
            description: Human-readable description of what's being selected.
            min_count: Minimum number of elements expected (default: 1).
            max_count: Maximum number of elements expected (None = unlimited).

        Returns:
            List of matching CheckedHtmlElements. Each element is wrapped to support
            nested checked queries.

        Raises:
            HTMLStructuralAssumptionException: If count doesn't match expectations.

        Example::

            tree = CheckedHtmlElement(lxml.html.fromstring(html))
            # Expect exactly 1 case name
            case_name = tree.checked_css("h1.case-name", "case name")
            # Expect at least 5 case divs
            cases = tree.checked_css("div.case", "case divs", min_count=5)
            # Nested queries work
            for case in cases:
                title = case.checked_css("h2.title", "title", min_count=1)
        """
        # Use lxml's built-in cssselect() method
        try:
            results = self._element.cssselect(selector)
        except Exception as e:
            # If CSS selector is invalid, raise with helpful context
            raise HTMLStructuralAssumptionException(
                selector=selector,
                selector_type="css",
                description=description,
                expected_min=min_count,
                expected_max=max_count,
                actual_count=0,
                request_url=self._request_url,
            ) from e

        # Report to active observer if present
        observer = get_active_observer()
        if observer is not None:
            observer.record_query(
                selector=selector,
                selector_type="css",
                description=description,
                results=list(results),  # Convert to list for consistency
                expected_min=min_count,
                expected_max=max_count,
                parent_element=self._element,
            )

        # Validate count
        actual_count = len(results)
        if actual_count < min_count or (
            max_count is not None and actual_count > max_count
        ):
            raise HTMLStructuralAssumptionException(
                selector=selector,
                selector_type="css",
                description=description,
                expected_min=min_count,
                expected_max=max_count,
                actual_count=actual_count,
                request_url=self._request_url,
            )

        # Wrap results in CheckedHtmlElement for nested queries
        # CSS selectors always return elements (never text/attributes)
        wrapped_results = [
            CheckedHtmlElement(result, self._request_url) for result in results
        ]

        return wrapped_results  # type: ignore[return-value]

    def __getattr__(self, name: str):
        """Delegate all other attributes to the wrapped element.

        This allows CheckedHtmlElement to be used as a drop-in replacement for
        HtmlElement, while adding the checked methods.
        """
        return getattr(self._element, name)
