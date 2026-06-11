"""Response validation operations for SQLManager."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from lxml import etree
from lxml import html as lxml_html
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlmodel import col

from jkent.common.exceptions import PersistentException
from jkent.driver.database_engine.compression import (
    decompress_response,
)
from jkent.driver.database_engine.errors import store_error
from jkent.driver.database_engine.models import Request

if TYPE_CHECKING:
    import asyncio

    from jkent.driver.database_engine.scoped_session import (
        ScopedSessionFactory,
    )

# Exceptions that mean the stored *data* is malformed, so the request_id is a
# genuine validation failure. Anything else raised while validating (a corrupt
# blob, a missing compression dictionary, an unexpected validator bug) is an
# infrastructure problem, recorded as a persistent error rather than counted as
# invalid scraped data.
_DATA_VALIDATION_ERRORS = (
    ValidationError,
    json.JSONDecodeError,
    UnicodeDecodeError,
    etree.LxmlError,
)


class ValidationMixin:
    """JSON and XML response validation operations."""

    _lock: asyncio.Lock  # type: ignore[misc]
    _session_factory: ScopedSessionFactory  # type: ignore[misc]

    async def _validate_responses_with(
        self,
        continuation: str,
        validator: Callable[[bytes], bool | None],
    ) -> list[int]:
        """Decompress each response for ``continuation`` and run ``validator(content)``.

        Returns the request_ids whose data is invalid: the validator returned
        ``False`` or raised a data-format error (see ``_DATA_VALIDATION_ERRORS``).
        Responses without compressed content are skipped. Failures that are not
        about the data — decompression errors, a missing dictionary, unexpected
        validator bugs — are recorded as persistent errors instead of being
        misreported as invalid data.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(
                    col(Request.id),
                    col(Request.content_compressed),
                    col(Request.compression_dict_id),
                ).where(
                    col(Request.continuation) == continuation,
                    col(Request.response_status_code).isnot(None),
                )
            )
            rows = result.all()

        invalid_request_ids: list[int] = []
        for row in rows:
            request_id, compressed_content, dict_id = row
            if compressed_content is None:
                continue

            # Decompression is infrastructure, not data validation.
            try:
                content = await decompress_response(
                    self._session_factory,
                    compressed_content,
                    dict_id,
                )
            except Exception as exc:
                await self._record_validation_infra_error(request_id, exc)
                continue

            try:
                if validator(content) is False:
                    invalid_request_ids.append(request_id)
            except _DATA_VALIDATION_ERRORS:
                invalid_request_ids.append(request_id)
            except Exception as exc:
                # An unexpected validator failure is not evidence the data is
                # invalid; surface it as a persistent error to investigate.
                await self._record_validation_infra_error(request_id, exc)

        return invalid_request_ids

    async def _record_validation_infra_error(
        self, request_id: int, exc: Exception
    ) -> None:
        """Record a non-data validation failure as a persistent error."""
        try:
            raise PersistentException(
                f"Response validation could not run for request "
                f"{request_id}: {exc}"
            ) from exc
        except PersistentException as wrapped:
            await store_error(
                self._session_factory,
                wrapped,
                request_id=request_id,
                db_lock=self._lock,
            )

    # --- JSON Response Validation ---

    async def validate_json_responses(
        self,
        continuation: str,
        model: type[BaseModel],
    ) -> list[int]:
        """Validate stored JSON responses against a Pydantic model.

        Args:
            continuation: The continuation method name to filter responses.
            model: Pydantic BaseModel class to validate against.

        Returns:
            List of request_id values for responses that failed validation.
        """

        def validate(content: bytes) -> None:
            model.model_validate(json.loads(content.decode("utf-8")))

        return await self._validate_responses_with(continuation, validate)

    # --- XML/XSD Response Validation ---

    async def validate_xml_responses(
        self,
        continuation: str,
        xsd_path: str,
    ) -> list[int]:
        """Validate stored HTML responses against an XSD schema.

        Args:
            continuation: The continuation method name to filter responses.
            xsd_path: Absolute path to the XSD schema file.

        Returns:
            List of request_id values for responses that failed validation.
        """
        schema = etree.XMLSchema(etree.parse(xsd_path))

        def validate(content: bytes) -> bool:
            return bool(schema.validate(lxml_html.fromstring(content)))

        return await self._validate_responses_with(continuation, validate)
