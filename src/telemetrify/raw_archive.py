"""zstd compression helpers for the raw_json archive column."""
import json
from typing import Any

import zstandard as zstd

_C = zstd.ZstdCompressor(level=3)
_D = zstd.ZstdDecompressor()


def compress(obj_or_str: Any) -> bytes:
    """Accepts a JSON-serializable object or a JSON string; returns zstd bytes."""
    if isinstance(obj_or_str, (bytes, bytearray)):
        data = bytes(obj_or_str)
    elif isinstance(obj_or_str, str):
        data = obj_or_str.encode("utf-8")
    else:
        data = json.dumps(obj_or_str, ensure_ascii=False).encode("utf-8")
    return _C.compress(data)


def decompress(blob: bytes | None) -> str | None:
    if blob is None:
        return None
    return _D.decompress(blob).decode("utf-8")


def decompress_json(blob: bytes | None) -> Any:
    s = decompress(blob)
    return None if s is None else json.loads(s)
