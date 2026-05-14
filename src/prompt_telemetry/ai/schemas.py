"""JSON schemas for structured AI outputs. Used by ai.client to validate
the parsed JSON against the expected shape; on mismatch the call is retried
once with a clarifying suffix, then marked failure.
"""
from __future__ import annotations

# Minimal schema language: a dict of {field: spec} where spec is one of:
#   {"type": "int", "min": N, "max": M}
#   {"type": "float", "min": F, "max": G}
#   {"type": "str", "max_len": L}
#   {"type": "bool"}
#   {"type": "enum", "values": [...]}
#   {"type": "obj", "fields": {...nested schema...}}
# Validation lives in client.validate_schema(); kept tiny on purpose (no pydantic).

GRADER = {
    "quality":          {"type": "int", "min": 1, "max": 5},
    "hallucination":    {"type": "enum", "values": ["low", "med", "high"]},
    "completeness":     {"type": "int", "min": 1, "max": 5},
    "refusal":          {"type": "bool"},
    "followed_request": {"type": "int", "min": 1, "max": 5},
    "notes":            {"type": "str", "max_len": 240},
}

CLUSTER_LABEL = {
    "label": {"type": "str", "max_len": 80},
}

RERUN_JUDGE = {
    "verdict":    {"type": "enum", "values": ["better", "same", "worse", "inconclusive"]},
    "confidence": {"type": "float", "min": 0.0, "max": 1.0},
    "reasoning":  {"type": "str", "max_len": 400},
    "dimensions": {"type": "obj", "fields": {
        "a": {"type": "obj", "fields": {
            "quality":      {"type": "int", "min": 1, "max": 5},
            "completeness": {"type": "int", "min": 1, "max": 5},
            "accuracy":     {"type": "int", "min": 1, "max": 5},
        }},
        "b": {"type": "obj", "fields": {
            "quality":      {"type": "int", "min": 1, "max": 5},
            "completeness": {"type": "int", "min": 1, "max": 5},
            "accuracy":     {"type": "int", "min": 1, "max": 5},
        }},
    }},
}

QA_PLANNER = {
    "semantic_query": {"type": "str", "max_len": 400},
    "filters":        {"type": "obj", "fields": {}},  # opaque — validated by parse_filters downstream
    "intent":         {"type": "enum", "values": ["find", "summarize", "count"]},
    "k":              {"type": "int", "min": 1, "max": 20},
}

QUEUE_RATIONALE = {
    "rationale": {"type": "str", "max_len": 240},
}

DIGEST = {
    "summary":     {"type": "str", "max_len": 1200},
    "top_clusters": {"type": "obj", "fields": {}},   # opaque list of {id, label, count}
    "regressions": {"type": "obj", "fields": {}},
    "suggestions": {"type": "obj", "fields": {}},
}

ANNOTATE_EXPECTED = {
    "expected_behavior": {"type": "str", "max_len": 600},
}

DIET = {
    "tightened_text":         {"type": "str", "max_len": 800},
    "predicted_savings_pct":  {"type": "float", "min": 0.0, "max": 100.0},
    "reasoning":              {"type": "str", "max_len": 400},
}
