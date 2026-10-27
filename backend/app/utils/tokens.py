"""Token counting + sliding-window chunking via tiktoken."""
from __future__ import annotations

from dataclasses import dataclass

import tiktoken

_ENCODER = None


def _enc():
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = tiktoken.get_encoding("cl100k_base")
    return _ENCODER


def count_tokens(text: str) -> int:
    return len(_enc().encode(text or ""))


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    enc = _enc()
    ids = enc.encode(text or "")
    if len(ids) <= max_tokens:
        return text
    return enc.decode(ids[:max_tokens])


@dataclass
class Chunk:
    index: int
    text: str
    token_count: int


def sliding_chunks(text: str, *, max_tokens: int = 500, overlap: int = 50) -> list[Chunk]:
    if not text:
        return []
    enc = _enc()
    ids = enc.encode(text)
    n = len(ids)
    if n == 0:
        return []
    if max_tokens <= 0:
        raise ValueError("max_tokens must be > 0")
    if overlap >= max_tokens:
        raise ValueError("overlap must be < max_tokens")

    chunks: list[Chunk] = []
    start = 0
    idx = 0
    step = max_tokens - overlap
    while start < n:
        end = min(start + max_tokens, n)
        slice_ids = ids[start:end]
        chunks.append(Chunk(index=idx, text=enc.decode(slice_ids), token_count=len(slice_ids)))
        idx += 1
        if end >= n:
            break
        start += step
    return chunks
