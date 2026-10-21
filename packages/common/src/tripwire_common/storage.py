"""Object storage for raw Parquet and model artifacts.

Paths are relative, '/'-separated keys (e.g. 'sec/submissions/cik=0000320193/sha256=.../x.parquet').
Raw data is append-only: writing a key that already holds different bytes is an error.
Only a local backend exists for now; Supabase Storage plugs in behind the same interface.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Protocol

from tripwire_common.settings import ConfigError, Settings, get_settings


class StorageError(RuntimeError):
    pass


class Storage(Protocol):
    def write_bytes(self, key: str, data: bytes) -> None: ...
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

    def read_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise StorageError(f"no such object: {key}")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def uri(self, key: str) -> str:
        return self._path(key).as_uri()


def get_storage(settings: Settings | None = None) -> Storage:
    settings = settings or get_settings()
    if settings.tripwire_storage == "local":
        return LocalStorage(settings.tripwire_data_dir)
    raise ConfigError(f"storage backend {settings.tripwire_storage!r} is not implemented yet")
