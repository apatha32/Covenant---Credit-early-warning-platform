"""SupabaseStorage against a fake Storage API (httpx.MockTransport)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tripwire_common.storage import StorageError, SupabaseStorage


class FakeStorageApi:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.buckets: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer service-key"
        path = request.url.path.removeprefix("/storage/v1")
        if path == "/bucket":
            if "b" in self.buckets:
                return httpx.Response(400, json={"message": "The resource already exists"})
            self.buckets.add("b")
            return httpx.Response(200, json={"name": "b"})
        key = path.removeprefix("/object/b/")
        if request.method == "POST":
            if key in self.objects:
                body = {"statusCode": "409", "message": "The resource already exists"}
                return httpx.Response(400, json=body)
            self.objects[key] = request.content
            return httpx.Response(200, json={"Key": key})
        if key not in self.objects:
            return httpx.Response(400, json={"statusCode": "404", "message": "Object not found"})
        return httpx.Response(200, content=self.objects[key] if request.method == "GET" else b"")


@pytest.fixture
def api() -> FakeStorageApi:
    return FakeStorageApi()


def _storage(api: FakeStorageApi, tmp_path: Path) -> SupabaseStorage:
    transport = httpx.MockTransport(api)
    return SupabaseStorage("http://x", "service-key", "b", tmp_path, transport=transport)


def test_write_read_exists(api: FakeStorageApi, tmp_path: Path) -> None:
    s = _storage(api, tmp_path)
    s.ensure_bucket()
    s.ensure_bucket()  # idempotent
    s.write_bytes("models/6/meta.json", b"{}")
    assert s.exists("models/6/meta.json")
    assert not s.exists("models/6/other")
    assert s.read_bytes("models/6/meta.json") == b"{}"
    assert s.local_path("models/6/meta.json").read_bytes() == b"{}"
    assert s.uri("models/6/meta.json") == "supabase://b/models/6/meta.json"


def test_append_only(api: FakeStorageApi, tmp_path: Path) -> None:
    s = _storage(api, tmp_path)
    s.write_bytes("k", b"same")
    s.write_bytes("k", b"same")  # identical re-write is a no-op
    with pytest.raises(StorageError, match="refusing to overwrite"):
        s.write_bytes("k", b"different")
    with pytest.raises(StorageError, match="no such object"):
        s.read_bytes("missing")
    with pytest.raises(StorageError, match="invalid storage key"):
        s.write_bytes("../escape", b"x")
