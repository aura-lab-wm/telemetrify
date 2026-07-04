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


# ---------------------------------------------------------------------------
# BUG 5 — --backfill-budget override spend must be scoped to the CURRENT
# invocation, not a lifetime-cumulative sum across every override run ever
# made (a later --backfill-budget run used to fail immediately because of a
# completely unrelated EARLIER run's spend).
# ---------------------------------------------------------------------------

def _seed_override_row(db, cost_usd: float):
    db.execute(
        "INSERT INTO ai_runs(feature, model, prompt_version, status, "
        "started_at, cost_usd, backend, override_budget) "
        "VALUES('grader', 'claude-sonnet-4-6', 'v1', 'success', "
        "datetime('now'), ?, 'anthropic', 1)",
        (cost_usd,),
    )
    db.commit()


def test_override_budget_ignores_earlier_unrelated_invocations(monkeypatch, migrated_db):
    """Simulate an EARLIER --backfill-budget run that already spent $4.99 of
    override budget (rows already sitting in the table before THIS
    invocation's session marker is captured). A brand-new invocation with its
    own $5.00 override cap must NOT see that old spend — otherwise it fails
    immediately, per BUG 5."""
    from telemetrify.ai.client import AnthropicClient

    # Reset the process-wide session marker so this test starts fresh
    # regardless of what other tests in this process already touched.
    monkeypatch.setattr(AnthropicClient, "_override_session_start_id", None)

    # Rows from a totally unrelated, already-finished earlier invocation.
    _seed_override_row(migrated_db, 4.99)

    client = AnthropicClient(migrated_db, override_budget_usd=5.00)
    spent = client._override_spend_usd()
    assert spent == pytest.approx(0.0), (
        "override spend must be scoped to THIS invocation only — an "
        "earlier, unrelated --backfill-budget run's rows must not count"
    )

    # And a call estimated well within the fresh $5.00 cap must not raise.
    client._check_budget(0.10)


def test_override_budget_accumulates_within_the_same_invocation(monkeypatch, migrated_db):
    """Rows written AFTER this invocation's session marker (i.e. by this
    same run) DO count — the fix scopes to "this invocation", not "nothing
    ever counts"."""
    from telemetrify.ai.client import AnthropicClient, BudgetExceeded

    monkeypatch.setattr(AnthropicClient, "_override_session_start_id", None)
    monkeypatch.setattr(AnthropicClient, "DEFAULT_DAILY_CAP_USD", 999.0)

    client = AnthropicClient(migrated_db, override_budget_usd=1.00)
    # First check establishes this invocation's baseline marker.
    client._check_budget(0.10)

    # A row written by "this same invocation" (id > the marker).
    _seed_override_row(migrated_db, 0.95)

    with pytest.raises(BudgetExceeded):
        client._check_budget(0.10)  # 0.95 + 0.10 > 1.00 cap
