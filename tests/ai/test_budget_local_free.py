"""Daily $ cap counts only Anthropic rows.

Seed 100 rocco rows totaling $0.00002, set the daily cap to $0.01, and assert
the next rocco call still proceeds and the next Anthropic call also proceeds —
because `_check_budget` filters on `backend='anthropic'`.
"""
from __future__ import annotations

import sqlite3

import pytest


def _seed_rocco_rows(db, n: int = 100, per_cost: float = 0.0000002):
    for i in range(n):
        db.execute(
            "INSERT INTO ai_runs(feature, model, prompt_version, status, "
            "started_at, cost_usd, backend) "
            "VALUES('qa', 'rocco-model', 'v1', 'success', "
            "datetime('now'), ?, 'rocco')",
            (per_cost,),
        )
    db.commit()


def test_local_rows_do_not_count_against_anthropic_cap(monkeypatch, migrated_db):
    """100 rocco rows pre-seeded; the cap is $0.01; budget check must
    return spend=$0 because rocco rows are filtered out."""
    from telemetrify.ai.client import AnthropicClient

    _seed_rocco_rows(migrated_db, n=100, per_cost=0.0000002)

    # Force a tiny cap.
    monkeypatch.setattr(AnthropicClient, "DEFAULT_DAILY_CAP_USD", 0.01)

    client = AnthropicClient(migrated_db)
    spent = client._today_spend_usd()
    # All 100 rows are rocco → counted spend must be 0.0
    assert spent == pytest.approx(0.0)

    # And _check_budget for a small Anthropic call must not raise.
    client._check_budget(0.001)  # well under $0.01 cap


def test_anthropic_rows_do_count_against_cap(monkeypatch, migrated_db):
    """Sanity check the inverse: an Anthropic row at $0.009 + 0.002 estimate
    should trip a $0.01 cap."""
    from telemetrify.ai.client import AnthropicClient, BudgetExceeded

    migrated_db.execute(
        "INSERT INTO ai_runs(feature, model, prompt_version, status, "
        "started_at, cost_usd, backend) "
        "VALUES('qa', 'claude-sonnet-4-6', 'v1', 'success', "
        "datetime('now'), 0.009, 'anthropic')"
    )
    migrated_db.commit()

    monkeypatch.setattr(AnthropicClient, "DEFAULT_DAILY_CAP_USD", 0.01)
    client = AnthropicClient(migrated_db)

    with pytest.raises(BudgetExceeded):
        client._check_budget(0.002)
