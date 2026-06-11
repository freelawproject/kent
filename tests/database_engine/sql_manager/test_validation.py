"""Tests for JSON/XML response validation (_validation.py)."""

from __future__ import annotations

import json

from pydantic import BaseModel

from jkent.driver.database_engine.compression import compress
from jkent.driver.database_engine.sql_manager import SQLManager


class TestValidateJSONResponses:
    """Tests for validate_json_responses diagnostic function."""

    async def test_validate_json_responses_all_valid(
        self, sql_manager: SQLManager
    ) -> None:
        """Test validation with all responses valid."""

        class TestModel(BaseModel):
            name: str
            count: int

        # Create requests and responses
        for i in range(3):
            request_id = await sql_manager.insert_request(
                priority=5,
                request_type="navigating",
                method="GET",
                url=f"https://example.com/{i}",
                headers_json=None,
                cookies_json=None,
                body=None,
                continuation="parse_api",
                current_location="",
                accumulated_data_json=None,
                permanent_json=None,
                expected_type=None,
                dedup_key=f"{i}",
                parent_id=None,
            )

            # Store valid JSON response
            content = json.dumps({"name": f"item_{i}", "count": i}).encode()
            compressed = compress(content)

            await sql_manager.store_response(
                request_id=request_id,
                status_code=200,
                headers_json=None,
                url=f"https://example.com/{i}",
                compressed_content=compressed,
                content_size_original=len(content),
                content_size_compressed=len(compressed),
                dict_id=None,
                continuation="parse_api",
            )

        # Validate - should return empty list (all valid)
        invalid_ids = await sql_manager.validate_json_responses(
            "parse_api", TestModel
        )
        assert invalid_ids == []

    async def test_validate_json_responses_some_invalid(
        self, sql_manager: SQLManager
    ) -> None:
        """Test validation with some invalid responses."""

        class TestModel(BaseModel):
            name: str
            count: int

        # Create valid response
        request_id_1 = await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/valid",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse_api",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key="valid",
            parent_id=None,
        )

        content_valid = json.dumps({"name": "valid", "count": 1}).encode()
        compressed_valid = compress(content_valid)

        await sql_manager.store_response(
            request_id=request_id_1,
            status_code=200,
            headers_json=None,
            url="https://example.com/valid",
            compressed_content=compressed_valid,
            content_size_original=len(content_valid),
            content_size_compressed=len(compressed_valid),
            dict_id=None,
            continuation="parse_api",
        )

        # Create invalid response (missing required field)
        request_id_2 = await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/invalid",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse_api",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key="invalid",
            parent_id=None,
        )

        content_invalid = json.dumps(
            {"name": "invalid"}
        ).encode()  # Missing count
        compressed_invalid = compress(content_invalid)

        await sql_manager.store_response(
            request_id=request_id_2,
            status_code=200,
            headers_json=None,
            url="https://example.com/invalid",
            compressed_content=compressed_invalid,
            content_size_original=len(content_invalid),
            content_size_compressed=len(compressed_invalid),
            dict_id=None,
            continuation="parse_api",
        )

        # Validate - should return request_id_2
        invalid_ids = await sql_manager.validate_json_responses(
            "parse_api", TestModel
        )
        assert invalid_ids == [request_id_2]

    async def test_validate_json_responses_no_responses(
        self, sql_manager: SQLManager
    ) -> None:
        """Test validation with no responses for continuation."""

        class TestModel(BaseModel):
            name: str

        # Validate nonexistent continuation - should return empty list
        invalid_ids = await sql_manager.validate_json_responses(
            "nonexistent", TestModel
        )
        assert invalid_ids == []

    async def test_validate_json_responses_malformed_json(
        self, sql_manager: SQLManager
    ) -> None:
        """Test validation with malformed JSON."""

        class TestModel(BaseModel):
            name: str

        request_id = await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/malformed",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse_api",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key="malformed",
            parent_id=None,
        )

        # Store invalid JSON
        content = b"not valid json at all"
        compressed = compress(content)

        await sql_manager.store_response(
            request_id=request_id,
            status_code=200,
            headers_json=None,
            url="https://example.com/malformed",
            compressed_content=compressed,
            content_size_original=len(content),
            content_size_compressed=len(compressed),
            dict_id=None,
            continuation="parse_api",
        )

        # Validate - should return request_id due to JSON parse error
        invalid_ids = await sql_manager.validate_json_responses(
            "parse_api", TestModel
        )
        assert invalid_ids == [request_id]

    async def test_validate_json_responses_empty_content(
        self, sql_manager: SQLManager
    ) -> None:
        """Test validation with empty/null response content."""

        class TestModel(BaseModel):
            name: str

        request_id = await sql_manager.insert_request(
            priority=5,
            request_type="navigating",
            method="GET",
            url="https://example.com/empty",
            headers_json=None,
            cookies_json=None,
            body=None,
            continuation="parse_api",
            current_location="",
            accumulated_data_json=None,
            permanent_json=None,
            expected_type=None,
            dedup_key="empty",
            parent_id=None,
        )

        # Store empty response (None content)
        await sql_manager.store_response(
            request_id=request_id,
            status_code=204,
            headers_json=None,
            url="https://example.com/empty",
            compressed_content=None,
            content_size_original=0,
            content_size_compressed=0,
            dict_id=None,
            continuation="parse_api",
        )

        # Validate - should skip empty content (return empty list)
        invalid_ids = await sql_manager.validate_json_responses(
            "parse_api", TestModel
        )
        assert invalid_ids == []
