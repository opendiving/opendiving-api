"""An in-memory stand-in for the boto3 S3 client, so the object-store backend can be
tested without a network, a bucket or a skip.

Six operations, which is all `services/blob_store.py` calls: `put_object`, `get_object`,
`delete_object`, `delete_objects`, `head_object` and `list_objects_v2` (plus its paginator).
Errors come back as botocore's real `ClientError` carrying the codes a store returns, so the
module under test catches what it would catch in production rather than something shaped
like it.

**What this cannot prove**, stated here because a fake that looks this much like the real
thing invites the assumption that it does: nothing about the wire protocol, the signature,
or any particular store's quirks. Those belong to boto3 and to whoever points this at a
live bucket. What it does prove is the part that is this repo's - the key mapping and the
prefix, which errors mean "gone" rather than "broken", that the emptiness check asks for one
key, and that a committed transaction's deletes actually reach the store.

`moto` would have covered more, at the price of a large dev dependency emulating the whole
of S3 for six calls. See *"A second backend, because the hosted disk is not shared"* in
`DECISIONS.md`.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr

from src.app.core.config import FileStorageBackendOption
from src.app.services import blob_store

#: Deliberately small, so that any listing of more than two objects exercises the paginator
#: rather than fitting in one page by accident.
PAGE_SIZE = 2


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class _Body:
    """What `get_object` hands back under `"Body"`: a stream the caller reads and closes."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        self.closed = True


class _Paginator:
    def __init__(self, client: FakeS3Client) -> None:
        self._client = client

    def paginate(self, **kwargs: Any) -> Any:
        prefix = kwargs.get("Prefix", "")
        self._client.record("list_objects_v2_paginate", kwargs)
        keys = self._client.matching(prefix)
        if not keys:
            # A real listing of nothing is still one page, with no `Contents` at all - not
            # zero pages. Callers that assume otherwise break on an empty bucket only.
            yield {"KeyCount": 0}
            return
        for start in range(0, len(keys), PAGE_SIZE):
            page = keys[start : start + PAGE_SIZE]
            yield {"KeyCount": len(page), "Contents": [{"Key": key} for key in page]}


class FakeS3Client:
    """One bucket's worth of bytes in a dict, answering the boto3 S3 client's surface.

    `calls` records every operation with its keyword arguments, which is how a test asserts
    something about the *request* rather than the result - that the emptiness check passes
    `MaxKeys=1`, say, or that a batch delete went out as one request rather than fifty.
    """

    def __init__(self, bucket: str = "test-bucket") -> None:
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}
        self.modified: dict[str, datetime] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: Object keys whose delete should report a failure, as an S3-compatible store does
        #: when it refuses one member of a batch.
        self.undeletable: set[str] = set()
        #: An exception to raise from the next call to the named operation.
        self.explode_on: dict[str, BaseException] = {}

    # -- bookkeeping -------------------------------------------------------------

    def record(self, operation: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((operation, kwargs))
        failure = self.explode_on.pop(operation, None)
        if failure is not None:
            raise failure

    def operations(self) -> list[str]:
        return [name for name, _ in self.calls]

    def seed(self, key: str, data: bytes = b"payload", *, modified: datetime | None = None) -> None:
        """Put an object there without recording a call, for arranging a test's starting
        state. Writing `objects[key]` by hand works too and is fine where the modification
        time is never asked for."""
        self.objects[key] = data
        self.modified[key] = modified or datetime.now(UTC)

    def matching(self, prefix: str, *, start_after: str = "") -> list[str]:
        """The bucket's keys under `prefix`, in the lexicographic order a real listing uses -
        which `StartAfter` then cuts into, exclusive of the marker itself."""
        return sorted(key for key in self.objects if key.startswith(prefix) and key > start_after)

    def _check_bucket(self, bucket: str, operation: str) -> None:
        if bucket != self.bucket:
            raise _client_error("NoSuchBucket", operation)

    # -- the client's surface ----------------------------------------------------

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.record("put_object", kwargs)
        self._check_bucket(kwargs["Bucket"], "PutObject")
        self.objects[kwargs["Key"]] = bytes(kwargs["Body"])
        self.modified[kwargs["Key"]] = datetime.now(UTC)
        return {}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.record("get_object", kwargs)
        self._check_bucket(kwargs["Bucket"], "GetObject")
        try:
            data = self.objects[kwargs["Key"]]
        except KeyError:
            raise _client_error("NoSuchKey", "GetObject") from None
        return {"Body": _Body(data)}

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.record("head_object", kwargs)
        self._check_bucket(kwargs["Bucket"], "HeadObject")
        key = kwargs["Key"]
        if key not in self.objects:
            # A HEAD response has no body to carry an error code, so botocore reports the
            # status: `404`, not `NoSuchKey`. Getting this wrong is how `exists()` turns a
            # missing object into a 500.
            raise _client_error("404", "HeadObject")
        # A real store always has a modification time; `.get` covers an object a test put
        # in the dict directly rather than through `seed` or `put_object`.
        modified = self.modified.get(key) or datetime.now(UTC)
        return {"LastModified": modified, "ContentLength": len(self.objects[key])}

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.record("delete_object", kwargs)
        self._check_bucket(kwargs["Bucket"], "DeleteObject")
        # `DeleteObject` succeeds on a key that was never there, which is what makes the
        # post-commit delete and the sweeper's retries free.
        self.objects.pop(kwargs["Key"], None)
        self.modified.pop(kwargs["Key"], None)
        return {}

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        self.record("delete_objects", kwargs)
        self._check_bucket(kwargs["Bucket"], "DeleteObjects")
        errors: list[dict[str, str]] = []
        for entry in kwargs["Delete"]["Objects"]:
            key = entry["Key"]
            if key in self.undeletable:
                errors.append({"Key": key, "Code": "AccessDenied"})
                continue
            self.objects.pop(key, None)
            self.modified.pop(key, None)
        return {"Errors": errors} if errors else {}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.record("list_objects_v2", kwargs)
        self._check_bucket(kwargs["Bucket"], "ListObjectsV2")
        keys = self.matching(kwargs.get("Prefix", ""), start_after=kwargs.get("StartAfter", ""))[
            : kwargs.get("MaxKeys", 1000)
        ]
        response: dict[str, Any] = {"KeyCount": len(keys)}
        if keys:
            response["Contents"] = [{"Key": key} for key in keys]
        return response

    def get_paginator(self, operation: str) -> _Paginator:
        assert operation == "list_objects_v2", f"nothing paginates {operation}"
        return _Paginator(self)


def select_s3_backend(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bucket: str = "opendiving-files",
    prefix: str | None = None,
) -> FakeS3Client:
    """Point `blob_store` at a fresh stub for the duration of one test.

    Everything `monkeypatch` sets here is restored afterwards, including the backend
    setting - so a test that takes this fixture runs on the object store and every other
    test in the session still runs on the volume.
    """
    client = FakeS3Client(bucket)
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_BACKEND", FileStorageBackendOption.S3)
    monkeypatch.setattr(blob_store.settings, "S3_ENDPOINT_URL", "https://example.invalid")
    monkeypatch.setattr(blob_store.settings, "S3_BUCKET", bucket)
    monkeypatch.setattr(blob_store.settings, "S3_ACCESS_KEY_ID", "an-access-key")
    monkeypatch.setattr(blob_store.settings, "S3_SECRET_ACCESS_KEY", SecretStr("a-secret"))
    monkeypatch.setattr(blob_store.settings, "S3_REGION", "auto")
    monkeypatch.setattr(blob_store.settings, "S3_PREFIX", prefix)
    monkeypatch.setattr(blob_store, "new_s3_client", lambda: client)
    # The live backend is cached on the settings that built it, so two tests configuring the
    # same endpoint would share one - and with it one stub. Clearing it is what makes each
    # test's stub the one that gets called.
    monkeypatch.setattr(blob_store, "_s3_cache", None)
    return client
