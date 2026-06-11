"""LxmlPageElement implementation wrapping CheckedHtmlElement.

This module provides an implementation of the PageElement protocol that wraps
CheckedHtmlElement and delegates to it. This is the standard implementation
used by all drivers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urljoin

from lxml import html

from jkent.common.checked_html import CheckedHtmlElement
from jkent.common.page_element import (
    Form,
    FormField,
    Link,
)

if TYPE_CHECKING:
    from jkent.common.selector_observer import (
        SelectorObserver,
    )


class LxmlPageElement:
    """Implementation of PageElement protocol wrapping CheckedHtmlElement.

    This is the standard implementation used by all drivers. It wraps
    CheckedHtmlElement to provide the PageElement interface.

    Attributes:
        _element: The underlying CheckedHtmlElement.
        _url: The base URL for resolving relative URLs.
        _observer: Optional SelectorObserver for query recording.
    """

    def __init__(
        self,
        element: CheckedHtmlElement,
        url: str = "",
        observer: SelectorObserver | None = None,
    ):
        """Initialize LxmlPageElement.

        Args:
            element: The CheckedHtmlElement to wrap.
            url: Base URL for resolving relative URLs.
            observer: Optional SelectorObserver for recording queries.
        """
        self._element = element
        self._url = url
        self._observer = observer

    def query_xpath(
        self,
        selector: str,
        description: str,
        min_count: int = 1,
        max_count: int | None = None,
    ) -> list[LxmlPageElement]:
        """Query elements by XPath selector.

        Args:
            selector: XPath expression to execute.
            description: Human-readable description of what's being selected.
            min_count: Minimum number of elements expected (default: 1).
            max_count: Maximum number of elements expected (None = unlimited).

        Returns:
            List of matching LxmlPageElement instances.

        Raises:
            HTMLStructuralAssumptionException: If count doesn't match expectations.
        """
        # Delegate to CheckedHtmlElement
        checked_elements = self._element.checked_xpath(
            selector, description, min_count, max_count
        )

        # Wrap each result in LxmlPageElement, inheriting the observer
        return [
            LxmlPageElement(elem, self._url, self._observer)
            for elem in checked_elements
        ]

    def query_xpath_strings(
        self,
        selector: str,
        description: str,
        min_count: int = 1,
        max_count: int | None = None,
    ) -> list[str]:
        """Query string values by XPath selector.

        Args:
            selector: XPath expression returning strings (text nodes, attributes).
            description: Human-readable description of what's being selected.
            min_count: Minimum number of strings expected (default: 1).
            max_count: Maximum number of strings expected (None = unlimited).

        Returns:
            List of matching string values.

        Raises:
            HTMLStructuralAssumptionException: If count doesn't match expectations.
        """
        # Delegate to CheckedHtmlElement with type=str
        return self._element.checked_xpath(
            selector, description, min_count, max_count, type=str
        )

    def query_css(
        self,
        selector: str,
        description: str,
        min_count: int = 1,
        max_count: int | None = None,
    ) -> list[LxmlPageElement]:
        """Query elements by CSS selector.

        Args:
            selector: CSS selector expression.
            description: Human-readable description of what's being selected.
            min_count: Minimum number of elements expected (default: 1).
            max_count: Maximum number of elements expected (None = unlimited).

        Returns:
            List of matching LxmlPageElement instances.

        Raises:
            HTMLStructuralAssumptionException: If count doesn't match expectations.
        """
        # Delegate to CheckedHtmlElement
        checked_elements = self._element.checked_css(
            selector, description, min_count, max_count
        )

        # Wrap each result in LxmlPageElement, inheriting the observer
        return [
            LxmlPageElement(elem, self._url, self._observer)
            for elem in checked_elements
        ]

    def text_content(self) -> str:
        """Extract the visible text content.

        Returns:
            Visible text content of the element and its descendants.
        """
        return self._element.text_content()

    def get_attribute(self, name: str) -> str | None:
        """Extract an attribute value.

        Args:
            name: Name of the attribute.

        Returns:
            Value of the attribute, or None if it doesn't exist.
        """
        return self._element.get(name)

    def inner_html(self) -> str:
        """Get the inner HTML content.

        Returns:
            Inner HTML content of the element as a string.
        """
        # Use lxml's tostring to get inner HTML
        # Access the wrapped lxml element
        elem = self._element._element

        inner = "".join(
            html.tostring(child, encoding="unicode") for child in elem
        )
        return inner

    def tag_name(self) -> str:
        """Get the element's tag name.

        Returns:
            Tag name as a lowercase string (e.g., "div", "a", "form").
        """
        return self._element.tag.lower()

    def find_form(
        self,
        selector: str,
        description: str,
    ) -> Form:
        """Find a form by selector.

        Args:
            selector: XPath or CSS selector to find the form.
            description: Human-readable description of the form.

        Returns:
            Form value object with action, method, and fields.

        Raises:
            HTMLStructuralAssumptionException: If no form matches the selector.
        """
        # Try XPath first (if it looks like XPath), otherwise CSS
        if selector.startswith("//") or selector.startswith("."):
            form_elements = self.query_xpath(
                selector, description, min_count=1, max_count=1
            )
        else:
            form_elements = self.query_css(
                selector, description, min_count=1, max_count=1
            )

        form_elem = form_elements[0]

        # Extract form action and method
        action = form_elem.get_attribute("action") or ""
        method = (form_elem.get_attribute("method") or "GET").upper()

        # Resolve action URL against base URL
        if action:
            action = urljoin(self._url, action)
        else:
            action = self._url

        # Extract form fields
        fields: list[FormField] = []

        # Find all input, select, and textarea elements
        input_elements = form_elem.query_xpath(
            ".//input", "form inputs", min_count=0
        )
        select_elements = form_elem.query_xpath(
            ".//select", "form selects", min_count=0
        )
        textarea_elements = form_elem.query_xpath(
            ".//textarea", "form textareas", min_count=0
        )

        # Process input elements
        for input_elem in input_elements:
            name = input_elem.get_attribute("name")
            if not name:
                continue  # Skip inputs without names

            field_type = input_elem.get_attribute("type") or "text"
            value = input_elem.get_attribute("value")

            # Per HTML spec, unchecked radios/checkboxes contribute nothing to
            # form submission; omit them so request bodies match real browsers.
            if (
                field_type in ("radio", "checkbox")
                and input_elem.get_attribute("checked") is None
            ):
                continue

            # A checked checkbox without an explicit value submits as "on".
            if field_type == "checkbox" and value is None:
                value = "on"

            fields.append(
                FormField(name=name, field_type=field_type, value=value)
            )

        # Process select elements
        for select_elem in select_elements:
            name = select_elem.get_attribute("name")
            if not name:
                continue

            # Find selected option or first option
            options = select_elem.query_xpath(
                ".//option", "select options", min_count=0
            )
            option_values = [
                opt.get_attribute("value") or opt.text_content()
                for opt in options
            ]

            # Find selected value
            selected_options = select_elem.query_xpath(
                ".//option[@selected]", "selected option", min_count=0
            )
            if selected_options:
                value = (
                    selected_options[0].get_attribute("value")
                    or selected_options[0].text_content()
                )
            elif options:
                value = (
                    options[0].get_attribute("value")
                    or options[0].text_content()
                )
            else:
                value = None

            fields.append(
                FormField(
                    name=name,
                    field_type="select",
                    value=value,
                    options=option_values,
                )
            )

        # Process textarea elements
        for textarea_elem in textarea_elements:
            name = textarea_elem.get_attribute("name")
            if not name:
                continue

            value = textarea_elem.text_content()

            fields.append(
                FormField(name=name, field_type="textarea", value=value)
            )

        return Form(
            action=action, method=method, fields=fields, selector=selector
        )

    def find_links(
        self,
        selector: str,
        description: str,
        min_count: int = 1,
        max_count: int | None = None,
    ) -> list[Link]:
        """Find links matching a selector.

        Args:
            selector: XPath or CSS selector to find <a> elements.
            description: Human-readable description of the links.
            min_count: Minimum number of links expected (default: 1).
            max_count: Maximum number of links expected (None = unlimited).

        Returns:
            List of Link value objects with resolved URLs and text.

        Raises:
            HTMLStructuralAssumptionException: If count doesn't match expectations.
        """
        # Try XPath first (if it looks like XPath), otherwise CSS
        if selector.startswith("//") or selector.startswith("."):
            link_elements = self.query_xpath(
                selector, description, min_count, max_count
            )
        else:
            link_elements = self.query_css(
                selector, description, min_count, max_count
            )

        links: list[Link] = []
        for i, elem in enumerate(link_elements):
            href = elem.get_attribute("href")
            if not href:
                continue  # Skip links without href

            # Resolve URL against base URL
            url = urljoin(self._url, href)
            text = elem.text_content().strip()

            # Create a unique selector for this specific link
            # Use the original selector with positional predicate
            link_selector = f"({selector})[{i + 1}]"

            links.append(Link(url=url, text=text, selector=link_selector))

        return links

    def links(self) -> list[Link]:
        """Discover all links in the element.

        Returns:
            List of all <a> elements with href attributes as Link objects.
        """
        return self.find_links(".//a[@href]", "all links", min_count=0)
