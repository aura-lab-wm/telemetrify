"""Versioned prompt templates per feature.

Every persisted AI output stamps `prompt_version` so iterating a prompt doesn't
invalidate historical scores — they remain queryable under their old version.
"""
from __future__ import annotations

from dataclasses import dataclass


# Default model assignments. Override via env vars or per-call.
MODEL_HAIKU  = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_OPUS   = "claude-opus-4-7"


@dataclass(frozen=True)
class PromptTemplate:
    version: str
    model: str
    system: str
    user_template: str

    def render(self, **kwargs) -> tuple[str, str]:
        return self.system, self.user_template.format(**kwargs)


# ─── Round A1: auto-grader ────────────────────────────────────────────────
GRADER = PromptTemplate(
    version="grader-v1",
    model=MODEL_HAIKU,
    system=(
        "You are an LLM-as-Judge evaluating one assistant turn from a Claude Code "
        "session. Score on a calibrated 1-5 scale. A '3' is acceptable-but-not-great; "
        "a '5' is excellent. Be terse in notes (≤ 20 words). "
        "Return ONLY a single JSON object, no prose before or after."
    ),
    user_template=(
        "PROMPT (user):\n"
        "{user_text}\n\n"
        "RESPONSE (assistant, first 4000 chars):\n"
        "{assistant_text}\n\n"
        "CONTEXT:\n"
        "- model: {model}\n"
        "- tool_call_count: {tool_call_count}\n"
        "- attribution_skill: {attribution_skill}\n\n"
        "Return JSON:\n"
        "{{\n"
        "  \"quality\": <1-5>,\n"
        "  \"hallucination\": \"low\" | \"med\" | \"high\",\n"
        "  \"completeness\": <1-5>,\n"
        "  \"refusal\": <true|false>,\n"
        "  \"followed_request\": <1-5>,\n"
        "  \"notes\": \"<≤20 words>\"\n"
        "}}"
    ),
)


# ─── Round A2: cluster auto-label ─────────────────────────────────────────
CLUSTER_LABEL = PromptTemplate(
    version="cluster-label-v1",
    model=MODEL_SONNET,
    system=(
        "You read 5 example prompts from the same cluster and write a 2–7 word "
        "semantic label describing their shared intent. No quotes, no full stops, "
        "lowercase. Examples: 'git commit & push', 'fix python type hints', "
        "'explain regex backreferences', 'debug fastapi route'. "
        "Return ONLY a single JSON object."
    ),
    user_template=(
        "{example_1}\n"
        "---\n"
        "{example_2}\n"
        "---\n"
        "{example_3}\n"
        "---\n"
        "{example_4}\n"
        "---\n"
        "{example_5}\n\n"
        "Return JSON: {{\"label\": \"<2-7 lowercase words>\"}}"
    ),
)


# ─── Round A3: rerun comparison judge ─────────────────────────────────────
RERUN_JUDGE = PromptTemplate(
    version="rerun-judge-v1",
    model=MODEL_SONNET,
    system=(
        "You're comparing two assistant responses to the same user prompt. "
        "Decide whether response B is better, same, worse, or inconclusive vs A. "
        "Score each on quality / completeness / accuracy (1-5). Be calibrated: "
        "not all reruns are improvements; many are equivalent. "
        "Return ONLY a single JSON object."
    ),
    user_template=(
        "PROMPT:\n{user_text}\n\n"
        "RESPONSE A (original, {a_model}, {a_date}):\n{a_text}\n\n"
        "RESPONSE B (rerun, {b_model}, {b_date}):\n{b_text}\n\n"
        "Return JSON:\n"
        "{{\n"
        "  \"verdict\": \"better\" | \"same\" | \"worse\" | \"inconclusive\",\n"
        "  \"confidence\": <0.0-1.0>,\n"
        "  \"reasoning\": \"<≤40 words>\",\n"
        "  \"dimensions\": {{\n"
        "    \"a\": {{\"quality\": <1-5>, \"completeness\": <1-5>, \"accuracy\": <1-5>}},\n"
        "    \"b\": {{\"quality\": <1-5>, \"completeness\": <1-5>, \"accuracy\": <1-5>}}\n"
        "  }}\n"
        "}}"
    ),
)


# ─── Round B1: Ask-the-Ledger planner & synthesizer ──────────────────────
QA_PLANNER = PromptTemplate(
    # The planner emits a small JSON object — semantic query, optional
    # filters, intent label, k. Haiku handles this trivially. Sonnet was
    # ~5× more expensive on the OAuth bucket for zero quality benefit on
    # such a structured task, so switching saves the user's Claude
    # subscription rate-limit budget for the synthesizer where Sonnet
    # actually earns its keep.
    version="qa-planner-v2",
    model=MODEL_HAIKU,
    system=(
        "You translate a user's question about their telemetrify corpus into "
        "a structured retrieval plan. Output a semantic query (the text used for "
        "vector + BM25 search), optional metadata filters, an intent label, and a "
        "k (number of turns to retrieve, 5-15). Filters keys you may use: "
        "model, cwd_glob, skill, since (YYYY-MM-DD), until, has_error, "
        "has_followup, has_annotation, min_tokens, max_tokens. "
        "Return ONLY a single JSON object."
    ),
    user_template=(
        "QUESTION: {question}\n\n"
        "Return JSON:\n"
        "{{\n"
        "  \"semantic_query\": \"<rephrasing for retrieval>\",\n"
        "  \"filters\": {{...}},\n"
        "  \"intent\": \"find\" | \"summarize\" | \"count\",\n"
        "  \"k\": <5-15>\n"
        "}}"
    ),
)

QA_SYNTHESIZER = PromptTemplate(
    version="qa-synth-v1",
    model=MODEL_SONNET,
    system=(
        "You're answering a question over the user's own telemetrify corpus. "
        "Be direct and specific. Cite turns inline using the token [#N] where N is "
        "the turn id from the provided sources. Use Markdown. Keep it under ~300 "
        "words unless the question explicitly asks for more. If the sources don't "
        "support an answer, say so plainly."
    ),
    user_template=(
        "QUESTION: {question}\n\n"
        "SOURCES (top {k} matched turns):\n{sources_block}\n\n"
        "Answer with inline [#N] citations."
    ),
)


# ─── Round B2: queue rationale ───────────────────────────────────────────
QUEUE_RATIONALE = PromptTemplate(
    version="queue-rationale-v1",
    model=MODEL_HAIKU,
    system=(
        "You write one-line rationales (≤ 20 words) explaining why a prompt is "
        "worth replaying against the current Claude. Mention concrete signals "
        "(low quality, follow-up, age, cluster size). No fluff. "
        "Return ONLY a single JSON object."
    ),
    user_template=(
        "TURN: #{turn_id}\n"
        "PROMPT: {user_text}\n"
        "SIGNALS: quality={quality}, has_followup={has_followup}, "
        "days_old={days_old}, cluster_members={cluster_members}\n\n"
        "Return JSON: {{\"rationale\": \"<≤20 words>\"}}"
    ),
)


# ─── Round C1: daily digest ──────────────────────────────────────────────
DIGEST = PromptTemplate(
    version="digest-v1",
    model=MODEL_SONNET,
    system=(
        "You write a 4-6 sentence daily entry for a personal telemetrify "
        "logbook. The user is a CS professor using Claude Code. Tone: measured, "
        "instrumental, second-person. Mention concrete numbers (turns, top "
        "clusters, regressions). Highlight at most 3 things worth re-asking. "
        "Return ONLY a single JSON object."
    ),
    user_template=(
        "DATE: {date}\n"
        "STATS:\n"
        "- turns today: {turns_today}\n"
        "- tokens today: {tokens_today}\n"
        "- avg quality (auto-grade): {avg_quality}\n"
        "- correction rate: {correction_pct}%\n"
        "- top clusters today: {top_clusters_block}\n"
        "- regressions (turns whose grade < cluster avg): {regressions_block}\n"
        "- suggested re-asks: {suggestions_block}\n\n"
        "Return JSON:\n"
        "{{\n"
        "  \"summary\": \"<4-6 sentences>\",\n"
        "  \"top_clusters\": [...],\n"
        "  \"regressions\": [...],\n"
        "  \"suggestions\": [...]\n"
        "}}"
    ),
)


# ─── Round C2: suggest expected_behavior ─────────────────────────────────
ANNOTATE_EXPECTED = PromptTemplate(
    version="annotate-expected-v1",
    model=MODEL_HAIKU,
    system=(
        "Imagine the user flagged this response as not-quite-right. Write a "
        "1-2 sentence description of what the *expected* (better) behavior would "
        "have been. Specific, not generic. No filler ('I think', 'perhaps'). "
        "Return ONLY a single JSON object."
    ),
    user_template=(
        "PROMPT: {user_text}\n\n"
        "RESPONSE: {assistant_text}\n\n"
        "Return JSON: {{\"expected_behavior\": \"<1-2 sentences>\"}}"
    ),
)


# ─── Round C3: prompt diet ───────────────────────────────────────────────
DIET = PromptTemplate(
    version="diet-v1",
    model=MODEL_SONNET,
    system=(
        "You rewrite a frequently-asked prompt to be tighter and clearer, "
        "preserving intent. Cut filler, declare context once, prefer imperatives. "
        "Estimate token savings (percent reduction). "
        "Return ONLY a single JSON object."
    ),
    user_template=(
        "CLUSTER LABEL: {cluster_label}\n"
        "ORIGINAL (representative):\n{original}\n\n"
        "FIVE EXAMPLE MEMBERS:\n{members_block}\n\n"
        "Return JSON:\n"
        "{{\n"
        "  \"tightened_text\": \"<the better prompt>\",\n"
        "  \"predicted_savings_pct\": <0-100>,\n"
        "  \"reasoning\": \"<≤40 words why>\"\n"
        "}}"
    ),
)


# ─── Round D1: classify-unknown-network-ports ────────────────────────────
# Used by the rocco-pulse menubar's "Identify with AI" button to label
# random high-port listeners the local classifier table couldn't tag.
# Input: a list of ports + per-port metadata (proc name, owning user,
# HTTP banner snippet from the rocco-agent's probe). Output: structured
# guesses with confidence. Haiku-class because port classification is a
# pattern-matching task, not a reasoning one — keeps the OAuth bucket
# cheap.
CLASSIFY_PORTS = PromptTemplate(
    version="classify-ports-v1",
    model=MODEL_HAIKU,
    system=(
        "You identify network services from minimal evidence. Given a list "
        "of listening ports with optional process names, owning Linux "
        "users, and HTTP-banner probe text, label what each port is "
        "likely serving. Common categories include: zmq, redis, postgres, "
        "mysql, jupyter, vllm, ollama, ssh, http-api (Go), prometheus, "
        "grafana, gradio, streamlit, gpu-stats, ray, tensorboard, mlflow, "
        "wandb, ipython-kernel, ephemeral-rpc, unknown. Use specific "
        "labels when the banner is unambiguous (e.g. 'redis' if RESP "
        "fingerprint, 'postgres' if SSL-required line). If a Python "
        "training-style command is visible, prefer 'training-rpc' / "
        "'pytorch-rpc' over generic 'unknown'. Return ONLY a single JSON "
        "object — no prose."
    ),
    user_template=(
        "Classify these listening ports:\n"
        "{ports_block}\n\n"
        "Return JSON:\n"
        "{{\n"
        "  \"classifications\": [\n"
        "    {{\n"
        "      \"port\": <int>,\n"
        "      \"kind\": \"<short canonical label, lowercase, hyphens ok>\",\n"
        "      \"label\": \"<≤30-char friendly display name>\",\n"
        "      \"confidence\": \"high|medium|low\",\n"
        "      \"reasoning\": \"<≤20 words why>\"\n"
        "    }}\n"
        "  ]\n"
        "}}"
    ),
)
