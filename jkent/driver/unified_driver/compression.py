"""Zstd compression for unified-driver responses.

This module is a thin re-export of :mod:`jkent.driver.database_engine.compression`,
which is the single source of truth for zstd compression. It previously held a
verbatim migration copy of that implementation; keeping the import path while
delegating to the canonical module means the two can no longer drift.

Import from here when working in the unified driver; the behaviour is identical
to ``database_engine.compression``.
"""

from __future__ import annotations

from jkent.driver.database_engine.compression import (
    DEFAULT_COMPRESSION_LEVEL,
    DEFAULT_DICT_SIZE,
    compress,
    compress_response,
    decompress,
    decompress_response,
    get_compression_dict,
    get_dict_by_id,
    recompress_responses,
    train_compression_dict,
)

__all__ = [
    "DEFAULT_COMPRESSION_LEVEL",
    "DEFAULT_DICT_SIZE",
    "compress",
    "compress_response",
    "decompress",
    "decompress_response",
    "get_compression_dict",
    "get_dict_by_id",
    "recompress_responses",
    "train_compression_dict",
]
