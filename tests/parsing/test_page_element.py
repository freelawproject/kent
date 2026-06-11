"""Tests for PageElement protocol and value objects.

Tests protocol conformance and value object behavior.
"""

import pytest

from jkent.common.page_element import (
    Form,
    FormField,
    Link,
    ViaFormSubmit,
    ViaLink,
)
from jkent.data_types import HttpMethod


def test_via_link_frozen():
    """ViaLink should be a frozen dataclass."""
    via = ViaLink(selector="//a[@id='link1']", description="Test link")
    assert via.selector == "//a[@id='link1']"
    assert via.description == "Test link"

    # Should not be able to modify
    with pytest.raises(AttributeError):
        via.selector = "different"  # type: ignore[misc]


def test_via_form_submit_frozen():
    """ViaFormSubmit should be a frozen dataclass."""
    via = ViaFormSubmit(
        form_selector="//form[@id='search']",
        submit_selector=".//button[@type='submit']",
        field_data={"q": "test query"},
        description="Search form",
    )
    assert via.form_selector == "//form[@id='search']"
    assert via.submit_selector == ".//button[@type='submit']"
    assert via.field_data == {"q": "test query"}

    # Should not be able to modify
    with pytest.raises(AttributeError):
        via.description = "different"  # type: ignore[misc]


def test_form_field_frozen():
    """FormField should be a frozen dataclass."""
    field = FormField(
        name="email", field_type="input", value="test@example.com"
    )
    assert field.name == "email"
    assert field.field_type == "input"
    assert field.value == "test@example.com"
    assert field.options is None

    # With options
    select_field = FormField(
        name="state",
        field_type="select",
        value="CA",
        options=["CA", "NY", "TX"],
    )
    assert select_field.options == ["CA", "NY", "TX"]

    # Should not be able to modify
    with pytest.raises(AttributeError):
        field.value = "different"  # type: ignore[misc]


def test_form_get_field():
    """Form.get_field should return the field by name."""
    fields = [
        FormField(name="username", field_type="input", value="john"),
        FormField(name="password", field_type="password", value="secret"),
        FormField(name="remember", field_type="checkbox", value="true"),
    ]
    form = Form(
        action="https://example.com/login",
        method="POST",
        fields=fields,
        selector="//form[@id='login']",
    )

    # Find existing field
    username_field = form.get_field("username")
    assert username_field is not None
    assert username_field.name == "username"
    assert username_field.value == "john"

    # Non-existent field
    assert form.get_field("nonexistent") is None


def test_form_submit_post():
    """Form.submit should create a Request for POST forms."""
    fields = [
        FormField(name="username", field_type="input", value="john"),
        FormField(name="password", field_type="password", value="secret"),
    ]
    form = Form(
        action="https://example.com/login",
        method="POST",
        fields=fields,
        selector="//form[@id='login']",
    )

    request = form.submit()

    assert request.request.url == "https://example.com/login"
    assert request.request.method == HttpMethod.POST
    assert request.via is not None
    assert isinstance(request.via, ViaFormSubmit)
    assert request.via.form_selector == "//form[@id='login']"
    assert request.via.field_data == {"username": "john", "password": "secret"}


def test_form_submit_with_overrides():
    """Form.submit should merge field data with overrides."""
    fields = [
        FormField(name="username", field_type="input", value="john"),
        FormField(name="password", field_type="password", value="default"),
    ]
    form = Form(
        action="https://example.com/login",
        method="POST",
        fields=fields,
        selector="//form[@id='login']",
    )

    request = form.submit(data={"password": "newsecret"})

    assert request.via is not None
    assert isinstance(request.via, ViaFormSubmit)
    # Override should take precedence
    assert request.via.field_data == {
        "username": "john",
        "password": "newsecret",
    }


def test_form_submit_with_submit_selector():
    """Form.submit should include submit_selector in ViaFormSubmit."""
    fields = [
        FormField(name="q", field_type="input", value="search term"),
    ]
    form = Form(
        action="https://example.com/search",
        method="GET",
        fields=fields,
        selector="//form[@id='search']",
    )

    request = form.submit(submit_selector=".//button[@name='submit']")

    assert request.via is not None
    assert isinstance(request.via, ViaFormSubmit)
    assert request.via.submit_selector == ".//button[@name='submit']"


def test_link_follow():
    """Link.follow should create a Request."""
    link = Link(
        url="https://example.com/detail/123",
        text="Case Details",
        selector="//a[@class='case-link'][1]",
    )

    request = link.follow()

    assert request.request.url == "https://example.com/detail/123"
    assert request.request.method == HttpMethod.GET
    assert request.via is not None
    assert isinstance(request.via, ViaLink)
    assert request.via.selector == "//a[@class='case-link'][1]"
    assert request.via.description == "link: Case Details"


def test_link_frozen():
    """Link should be a frozen dataclass."""
    link = Link(
        url="https://example.com/page",
        text="Test Page",
        selector="//a[@id='link1']",
    )

    # Should not be able to modify
    with pytest.raises(AttributeError):
        link.url = "different"  # type: ignore[misc]


def test_form_frozen():
    """Form should be a frozen dataclass."""
    form = Form(
        action="https://example.com/submit",
        method="POST",
        fields=[],
        selector="//form",
    )

    # Should not be able to modify
    with pytest.raises(AttributeError):
        form.action = "different"  # type: ignore[misc]
