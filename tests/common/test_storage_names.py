from __future__ import annotations

from pathlib import Path

import pytest

from tripwire_common.names import normalize_company_name
from tripwire_common.storage import LocalStorage, StorageError


def test_local_storage_round_trip_and_same_content_rewrite(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    storage.write_bytes("a/b/c.parquet", b"x")
    storage.write_bytes("a/b/c.parquet", b"x")  # identical rewrite is a no-op
    assert storage.read_bytes("a/b/c.parquet") == b"x"
    assert storage.exists("a/b/c.parquet") and not storage.exists("a/b/d.parquet")


def test_local_storage_never_overwrites_raw_data(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    storage.write_bytes("k", b"original")
    with pytest.raises(StorageError, match="overwrite"):
        storage.write_bytes("k", b"changed")
    assert storage.read_bytes("k") == b"original"


@pytest.mark.parametrize("key", ["/abs", "../escape", "a/../../b", ""])
def test_local_storage_rejects_unsafe_keys(tmp_path: Path, key: str) -> None:
    with pytest.raises(StorageError):
        LocalStorage(tmp_path).write_bytes(key, b"x")


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("REVLON INC /DE/", "revlon"),
        ("Bed Bath & Beyond Inc.", "bed bath and beyond"),
        ("The Hertz Corporation", "hertz"),
        ("Hertz Global Holdings, Inc.", "hertz global holdings"),
        ("J. C. Penney Co., Inc.", "j c penney"),
        ("Corporate Office Properties Trust", "corporate office properties trust"),
        ("Inc", "inc"),  # never normalizes to empty
    ],
)
def test_normalize_company_name(raw: str, normalized: str) -> None:
    assert normalize_company_name(raw) == normalized
