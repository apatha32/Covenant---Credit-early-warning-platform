"""Object storage for raw Parquet and model artifacts.

Paths are relative, '/'-separated keys (e.g. 'sec/submissions/cik=0000320193/sha256=.../x.parquet').
Raw data is append-only: writing a key that already holds different bytes is an error.
Backends: LocalStorage (a directory, for development) and SupabaseStorage (a private bucket,
for the hosted demo and its scheduled refresh).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Protocol

import httpx

from tripwire_common.settings import ConfigError, Settings, get_settings


class StorageError(RuntimeError):
    pass


class Storage(Protocol):
    def write_bytes(self, key: str, data: bytes) -> None: ...
    def write_file(self, key: str, source: Path) -> None: ...
    def local_path(self, key: str) -> Path: ...
    def read_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def uri(self, key: str) -> str: ...


def _check_key(key: str) -> PurePosixPath:
    path = PurePosixPath(key)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise StorageError(f"invalid storage key: {key!r}")
    return path


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root.joinpath(*_check_key(key).parts)

    def write_bytes(self, key: str, data: bytes) -> None:
        path = self._path(key)
        if path.exists():
            if path.read_bytes() == data:
                return
            raise StorageError(f"refusing to overwrite existing object with new content: {key}")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file and rename, so a crash never leaves a half-written object.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def write_file(self, key: str, source: Path) -> None:
        """Move a large local file into storage without reading it into memory. Keys for big
        objects are content-addressed, so an existing key already holds the same bytes."""
        path = self._path(key)
        if path.exists():
            if path.stat().st_size != source.stat().st_size:
                raise StorageError(f"refusing to overwrite existing object with new content: {key}")
            source.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, path)

    def local_path(self, key: str) -> Path:
        """Filesystem path of an object, for streaming readers (e.g. zipfile)."""
        path = self._path(key)
        if not path.is_file():
            raise StorageError(f"no such object: {key}")
        return path

    def read_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise StorageError(f"no such object: {key}")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def uri(self, key: str) -> str:
        return self._path(key).as_uri()


class SupabaseStorage:
    """Objects in one private Supabase Storage bucket, reached with the service-role key.
    Same append-only rule as LocalStorage. local_path() downloads into a local cache."""

    def __init__(self, url: str, service_key: str, bucket: str, cache_dir: Path,
                 transport: httpx.BaseTransport | None = None) -> None:  # fmt: skip
        self.bucket = bucket
        self.cache_dir = cache_dir
        self.client = httpx.Client(
            base_url=f"{url.rstrip('/')}/storage/v1",
            headers={"Authorization": f"Bearer {service_key}", "apikey": service_key},
            timeout=httpx.Timeout(120, connect=10),
            transport=transport,
        )

    def _object(self, key: str) -> str:
        return f"/object/{self.bucket}/{_check_key(key)}"

    def ensure_bucket(self) -> None:
        r = self.client.post("/bucket", json={"id": self.bucket, "name": self.bucket,
                                               "public": False})  # fmt: skip
        if r.status_code not in (200, 201) and "already exists" not in r.text.lower():
            raise StorageError(f"cannot create bucket {self.bucket}: {r.status_code} {r.text}")

    def _upload(self, key: str, content: bytes) -> None:
        r = self.client.post(self._object(key), content=content,
                             headers={"Content-Type": "application/octet-stream",
                                      "x-upsert": "false"})  # fmt: skip
        if r.status_code in (200, 201):
            return
        if r.status_code in (400, 409) and "exist" in r.text.lower():
            if self.read_bytes(key) == content:
                return
            raise StorageError(f"refusing to overwrite existing object with new content: {key}")
        raise StorageError(f"upload failed for {key}: {r.status_code} {r.text}")

    def write_bytes(self, key: str, data: bytes) -> None:
        self._upload(key, data)

    def write_file(self, key: str, source: Path) -> None:
        self._upload(key, source.read_bytes())
        source.unlink()

    def read_bytes(self, key: str) -> bytes:
        r = self.client.get(self._object(key))
        if r.status_code in (400, 404):
            raise StorageError(f"no such object: {key}")
        r.raise_for_status()
        return r.content

    def exists(self, key: str) -> bool:
        r = self.client.head(self._object(key))
        return r.status_code == 200

    def local_path(self, key: str) -> Path:
        path = self.cache_dir / self.bucket / Path(*_check_key(key).parts)
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.read_bytes(key))
        return path

    def uri(self, key: str) -> str:
        return f"supabase://{self.bucket}/{_check_key(key)}"


def get_storage(settings: Settings | None = None) -> Storage:
    settings = settings or get_settings()
    if settings.tripwire_storage == "local":
        return LocalStorage(settings.tripwire_data_dir)
    if settings.tripwire_storage == "supabase":
        return SupabaseStorage(
            settings.require("supabase_url"),
            settings.require("supabase_service_role_key"),
            settings.supabase_storage_bucket,
            settings.tripwire_data_dir / ".storage-cache",
        )
    raise ConfigError(f"unknown storage backend {settings.tripwire_storage!r}")
