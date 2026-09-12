"""Regression tests for the `cache` decorator (behavior should be unaffected by its typing refactor)."""

import pathlib
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import Request

from src.app.core.exceptions.cache_exceptions import InvalidRequestError, MissingClientError
from src.app.core.utils import cache as cache_module
from src.app.core.utils.cache import _digest_sources, cache, namespaced


def _make_request(method: str) -> Request:
    request = Mock(spec=Request)
    request.method = method
    return request


def _sweeping_redis(*existing: str) -> Mock:
    """A Redis stand-in whose `scan` answers with `existing` once, then ends the cursor."""
    redis = Mock()
    scanned = [(0, [key.encode() for key in existing])]
    redis.scan = AsyncMock(side_effect=lambda *_args, **_kwargs: scanned.pop(0) if scanned else (0, []))
    redis.delete = AsyncMock(return_value=len(existing))
    return redis


class TestCacheDecoratorGet:
    @pytest.mark.asyncio
    async def test_returns_cached_data_without_calling_wrapped_func(self):
        mock_redis = Mock()
        mock_redis.get = AsyncMock(return_value=b'{"cached": true}')

        func = AsyncMock()

        with patch.object(cache_module, "client", mock_redis):
            decorated = cache(key_prefix="item")(func)
            result = await decorated(_make_request("GET"), id=1)

        assert result == {"cached": True}
        func.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_wrapped_func_and_caches_result_on_miss(self):
        mock_redis = Mock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.set = AsyncMock(return_value=True)
        mock_redis.expire = AsyncMock(return_value=True)

        func = AsyncMock(return_value={"value": 42})

        with patch.object(cache_module, "client", mock_redis):
            decorated = cache(key_prefix="item", expiration=120)(func)
            result = await decorated(_make_request("GET"), id=1)

        assert result == {"value": 42}
        func.assert_called_once()
        mock_redis.set.assert_called_once()
        cache_key = mock_redis.set.call_args[0][0]
        assert cache_key == namespaced("item:1")
        mock_redis.expire.assert_called_once_with(cache_key, 120)

    @pytest.mark.asyncio
    async def test_uses_resource_id_name_when_given(self):
        mock_redis = Mock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.set = AsyncMock(return_value=True)
        mock_redis.expire = AsyncMock(return_value=True)

        func = AsyncMock(return_value={"value": 1})

        with patch.object(cache_module, "client", mock_redis):
            decorated = cache(key_prefix="user_{user_id}_items", resource_id_name="item_id")(func)
            await decorated(_make_request("GET"), user_id=7, item_id=99)

        cache_key = mock_redis.set.call_args[0][0]
        assert cache_key == namespaced("user_7_items:99")

    @pytest.mark.asyncio
    async def test_raises_invalid_request_error_when_get_has_invalidation_config(self):
        mock_redis = Mock()

        func = AsyncMock()

        with (
            patch.object(cache_module, "client", mock_redis),
            pytest.raises(InvalidRequestError),
        ):
            decorated = cache(key_prefix="item", to_invalidate_extra={"other": "{id}"})(func)
            await decorated(_make_request("GET"), id=1)

        func.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_missing_client_error_when_client_not_initialized(self):
        func = AsyncMock()

        with (
            patch.object(cache_module, "client", None),
            pytest.raises(MissingClientError),
        ):
            decorated = cache(key_prefix="item")(func)
            await decorated(_make_request("GET"), id=1)

        func.assert_not_called()


class TestCacheDecoratorNonGet:
    @pytest.mark.asyncio
    async def test_deletes_cache_key_and_returns_raw_result(self):
        mock_redis = Mock()
        mock_redis.delete = AsyncMock(return_value=1)

        func = AsyncMock(return_value={"message": "updated"})

        with patch.object(cache_module, "client", mock_redis):
            decorated = cache(key_prefix="item")(func)
            result = await decorated(_make_request("PATCH"), id=1)

        assert result == {"message": "updated"}
        mock_redis.delete.assert_called_once_with(namespaced("item:1"))

    @pytest.mark.asyncio
    async def test_invalidates_extra_keys(self):
        mock_redis = Mock()
        mock_redis.delete = AsyncMock(return_value=1)

        func = AsyncMock(return_value={"status": "updated"})

        with patch.object(cache_module, "client", mock_redis):
            decorated = cache(
                key_prefix="item_data",
                resource_id_name="item_id",
                to_invalidate_extra={"user_items": "{user_id}"},
            )(func)
            await decorated(_make_request("PUT"), item_id=1, user_id=7)

        deleted_keys = {call.args[0] for call in mock_redis.delete.call_args_list}
        assert deleted_keys == {namespaced("item_data:1"), namespaced("user_items:7")}


class TestTheNamespaceIsTheBuild:
    """The guard for *"A deploy cannot serve the previous build's response cache"* in
    `DECISIONS.md`.

    What `@cache` stores is a response body, replayed without re-running the route, so an
    entry outliving the shape it was written for is a `ResponseValidationError` - a 500 for
    every reader of that endpoint until the TTL runs out, and up to an hour of it on the
    single-resource reads. Nothing else fails when the namespace stops separating builds:
    the tests above all pass, and so does every endpoint, right up until the deploy.
    """

    def test_the_key_carries_the_build(self):
        assert namespaced("user_7_dive:abc").startswith(f"resp:{cache_module._BUILD}:")
        assert namespaced("user_7_dive:abc").endswith(":user_7_dive:abc")

    @pytest.mark.asyncio
    async def test_another_builds_entry_is_not_a_hit(self):
        """The whole point: the previous build's body is unreachable rather than replayed."""
        stored = {"resp:an-older-build:item:1": b'{"shape": "yesterday"}'}
        mock_redis = Mock()
        mock_redis.get = AsyncMock(side_effect=lambda key: stored.get(key))
        mock_redis.set = AsyncMock(return_value=True)
        mock_redis.expire = AsyncMock(return_value=True)

        func = AsyncMock(return_value={"shape": "today"})

        with patch.object(cache_module, "client", mock_redis):
            decorated = cache(key_prefix="item")(func)
            result = await decorated(_make_request("GET"), id=1)

        assert result == {"shape": "today"}
        func.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_sweep_reaches_every_builds_entries(self):
        """A keyed delete is this build's; a *pattern* sweep is every build's.

        `erase_user` sweeps `user_{id}_*` and has to mean every cached row of that user's,
        not the ones this image happened to write - and an ordinary invalidation is as true
        of a superseded build's copy as of this one's.
        """
        older = "resp:an-older-build:user_7_dives:page_1"
        mine = namespaced("user_7_dives:page_2")
        mock_redis = _sweeping_redis(older, mine)

        with patch.object(cache_module, "client", mock_redis):
            await cache_module.delete_keys_by_pattern("user_7_dives:*")

        assert mock_redis.scan.call_args.kwargs["match"] == "resp:*:user_7_dives:*"
        mock_redis.delete.assert_awaited_once_with(older.encode(), mine.encode())


class TestTheSourceFingerprint:
    """`_digest_sources` is what gives a source checkout a build identity, and the only
    property that matters is that it **moves when the code moves**.

    Nothing else in the suite would notice it stopping: a digest that never changes still
    namespaces every key, still sweeps correctly, and still passes every test above - it just
    quietly serves the other branch's response bodies after a branch switch, which is the one
    failure this fallback exists for. An "optimisation" to hash mtimes, or only `schemas/`,
    is what that would look like in a diff.
    """

    def _tree(self, root, **files):
        for name, body in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        return root

    def test_the_same_sources_digest_the_same(self, tmp_path):
        self._tree(tmp_path, **{"a.py": "x = 1", "pkg/b.py": "y = 2"})
        assert _digest_sources(tmp_path) == _digest_sources(tmp_path)

    def test_editing_a_file_moves_it(self, tmp_path):
        self._tree(tmp_path, **{"a.py": "x = 1"})
        before = _digest_sources(tmp_path)
        (tmp_path / "a.py").write_text("x = 2")
        assert _digest_sources(tmp_path) != before

    def test_adding_a_file_moves_it(self, tmp_path):
        self._tree(tmp_path, **{"a.py": "x = 1"})
        before = _digest_sources(tmp_path)
        (tmp_path / "b.py").write_text("x = 1")
        assert _digest_sources(tmp_path) != before

    def test_moving_a_file_moves_it(self, tmp_path):
        """The path is hashed alongside the bytes, so a module that changes home changes the
        digest even though no file's contents did."""
        self._tree(tmp_path, **{"a.py": "x = 1"})
        before = _digest_sources(tmp_path)
        (tmp_path / "a.py").rename(tmp_path / "moved.py")
        assert _digest_sources(tmp_path) != before

    def test_a_tree_with_nothing_in_it_is_none(self, tmp_path):
        """A root that finds no sources must not answer with a digest. `rglob` on a missing
        directory yields nothing and raises nothing, so the digest of an empty run is a
        perfectly stable value that is the same everywhere - it would namespace every key and
        separate no builds, with nothing failing to say so."""
        assert _digest_sources(tmp_path / "does-not-exist") is None
        assert _digest_sources(tmp_path) is None

    def test_it_runs_at_import_so_it_never_raises(self, tmp_path):
        """Whatever it is pointed at, the answer is a digest or `None` - never an exception,
        because this is evaluated while the module is being imported."""
        (tmp_path / "a.py").write_text("x = 1")
        (tmp_path / "a.py").chmod(0o000)
        try:
            assert _digest_sources(tmp_path) is None
        finally:
            (tmp_path / "a.py").chmod(0o644)

    def test_the_real_package_is_what_gets_hashed(self):
        """`parents[2]` has to land on the `app` package itself. It is a positional walk up
        the tree, so moving this module one directory either way changes what it hashes."""
        from src.app.core.utils.cache import _source_fingerprint

        package = pathlib.Path(cache_module.__file__).resolve().parents[2]
        assert package.name == "app"
        assert (package / "main.py").is_file()
        assert _source_fingerprint() == _digest_sources(package)
        assert _source_fingerprint() is not None
