"""Unit tests for cache key helper utilities."""

import pytest

from src.app.core.exceptions.cache_exceptions import CacheIdentificationInferenceError
from src.app.core.utils.cache import (
    _construct_data_dict,
    _extract_data_inside_brackets,
    _format_extra_data,
    _format_prefix,
    _infer_resource_id,
)


class TestExtractDataInsideBrackets:
    def test_extracts_single_placeholder(self):
        assert _extract_data_inside_brackets("user_{user_id}_items") == ["user_id"]

    def test_extracts_multiple_placeholders(self):
        result = _extract_data_inside_brackets("The {quick} brown {fox} jumps over the {lazy} dog.")
        assert result == ["quick", "fox", "lazy"]

    def test_returns_empty_list_when_no_placeholders(self):
        assert _extract_data_inside_brackets("plain_prefix") == []


class TestConstructDataDict:
    def test_builds_dict_from_matching_kwargs(self):
        result = _construct_data_dict(["user_id", "item_id"], {"user_id": 1, "item_id": 2, "extra": 3})
        assert result == {"user_id": 1, "item_id": 2}

    def test_returns_empty_dict_for_no_keys(self):
        assert _construct_data_dict([], {"user_id": 1}) == {}


class TestFormatPrefix:
    def test_formats_prefix_with_kwargs(self):
        assert _format_prefix("user_{user_id}_items", {"user_id": 42}) == "user_42_items"

    def test_formats_prefix_without_placeholders(self):
        assert _format_prefix("static_prefix", {"user_id": 42}) == "static_prefix"


class TestFormatExtraData:
    def test_formats_extra_invalidation_targets(self):
        to_invalidate_extra = {"user_items": "{user_id}"}
        kwargs = {"user_id": 7, "item_id": 99}

        result = _format_extra_data(to_invalidate_extra, kwargs)

        assert result == {"user_items": 7}

    def test_formats_multiple_extra_targets(self):
        to_invalidate_extra = {"user_{user_id}_items": "{item_id}", "static_prefix": "{user_id}"}
        kwargs = {"user_id": 7, "item_id": 99}

        result = _format_extra_data(to_invalidate_extra, kwargs)

        assert result == {"user_7_items": 99, "static_prefix": 7}


class TestInferResourceId:
    def test_infers_int_id_from_kwargs(self):
        result = _infer_resource_id({"user_id": 5, "name": "bob"}, int)
        assert result == 5

    def test_infers_str_id_from_kwargs(self):
        result = _infer_resource_id({"slug": "my-post"}, str)
        assert result == "my-post"

    def test_raises_when_no_matching_id_found(self):
        with pytest.raises(CacheIdentificationInferenceError):
            _infer_resource_id({"name": "bob"}, int)
