"""Tests for the exception hierarchy."""

from jkent.common.exceptions import (
    HTTPResponseAssumptionException,
    TransientException,
)


def test_http_response_assumption_exception_is_transient():
    """Unexpected status codes raise the HTTP-named transient exception.

    The exception is about HTTP status codes, not HTML — the old
    HTMLResponseAssumptionException name was a misnomer.
    """
    exc = HTTPResponseAssumptionException(
        status_code=503,
        expected_codes=[200],
        url="https://example.com/cases",
    )

    assert isinstance(exc, TransientException)
    assert exc.status_code == 503
    assert "503" in exc.message
