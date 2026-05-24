"""OTLP export -> dash0_* tables. Walker stubs land in A.2; real shredding in A.3."""
from __future__ import annotations

import sqlite3
from typing import Any


def insert_trace_export(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    raw_body: bytes | None = None,
) -> int:
    """Insert all spans in an ExportTraceServiceRequest. Returns inserted-span count."""
    return 0


def insert_log_export(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
) -> int:
    """Insert all records in an ExportLogsServiceRequest. Returns inserted-record count."""
    return 0
