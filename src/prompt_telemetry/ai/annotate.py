"""Suggest expected_behavior for the annotation form. Cheap haiku call."""
from __future__ import annotations

import sqlite3

from . import prompts as P, schemas as S
from .client import AnthropicClient, BudgetExceeded


def suggest_expected(conn: sqlite3.Connection, turn_id: int) -> str | None:
    row = conn.execute(
        "SELECT user_text, assistant_text FROM turns WHERE id = ?", (turn_id,)
    ).fetchone()
    if not row or not row["user_text"] or not row["assistant_text"]:
        return None
    client = AnthropicClient(conn)
    try:
        res = client.call(
            feature="annotate",
            template=P.ANNOTATE_EXPECTED,
            user_kwargs={
                "user_text": row["user_text"].strip()[:2000],
                "assistant_text": row["assistant_text"].strip()[:2000],
            },
            schema=S.ANNOTATE_EXPECTED,
            target_id=turn_id,
            max_tokens=300, timeout=20.0,
        )
    except (BudgetExceeded, Exception):
        return None
    return (res.parsed.get("expected_behavior") or "").strip() or None
