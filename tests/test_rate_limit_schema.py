"""Unit tests for rate limit path sanitization and schema validation."""

from src.app.schemas.rate_limit import RateLimitBase, RateLimitUpdate, sanitize_path


class TestSanitizePath:
    def test_strips_leading_and_trailing_slashes(self):
        assert sanitize_path("/api/v1/users/") == "api_v1_users"

    def test_replaces_internal_slashes_with_underscores(self):
        assert sanitize_path("api/v1/users") == "api_v1_users"

    def test_leaves_path_without_slashes_unchanged(self):
        assert sanitize_path("users") == "users"


class TestRateLimitBaseValidation:
    def test_path_is_sanitized_on_construction(self):
        rate_limit = RateLimitBase(path="/api/v1/users/", limit=5, period=60)

        assert rate_limit.path == "api_v1_users"

    def test_limit_and_period_are_preserved(self):
        rate_limit = RateLimitBase(path="users", limit=10, period=3600)

        assert rate_limit.limit == 10
        assert rate_limit.period == 3600


class TestRateLimitUpdateValidation:
    def test_path_is_sanitized_when_provided(self):
        update = RateLimitUpdate(path="/api/v1/users/")

        assert update.path == "api_v1_users"

    def test_other_fields_default_to_none(self):
        update = RateLimitUpdate()

        assert update.path is None
        assert update.limit is None
        assert update.period is None
        assert update.name is None
