"""LLM-substrate for telemetrify v3.

All AI calls go through ai.client.AnthropicClient — single budget guard,
audit-logged via the ai_runs table, version-stamped via ai.prompts.

Modules:
    client          — AnthropicClient + BudgetExceeded + cost accounting
    prompts         — versioned prompt templates (system + user)
    schemas         — JSON schemas for structured outputs
    grader          — auto-grade a turn   (Round A1)
    cluster_label   — auto-label a cluster (Round A2)
    rerun_judge     — judge a rerun       (Round A3)
    qa              — Ask-the-Ledger      (Round B1)
    queue           — Smart Rerun Queue   (Round B2)
    digest          — Daily digest        (Round C1)
    annotate        — Suggest expected_behavior (Round C2)
    diet            — Prompt diet analyzer (Round C3)
    classifier      — Local sklearn grader fallback (parallel stream)
"""
