"""Local, fine-tunable grader classifier.

Learns a coarse 3-class quality bucket (low/mid/high) from `prompt_vec`
embeddings + a handful of turn-level metadata scalars. Trained against
`auto_grades` (silver, weight=1) and `annotations` (gold, weight=3); the
intent is to replace the LLM grader for high-volume scoring once enough
silver labels exist, with the option to keep tightening via gold labels.

Design choices (documented inline so a future maintainer doesn't have to
guess):

- **Model**: `sklearn.linear_model.LogisticRegression` with `multinomial`
  loss + L2. Embeddings are L2-normalized 384-dim sentence-transformer
  outputs; logistic regression on top of those is exactly the "linear
  probe" pattern. It trains in seconds even on tens of thousands of rows,
  inference is a single matmul (microseconds per row), and the calibrated
  `predict_proba` confidence is genuinely meaningful — unlike a
  gradient-boosted tree where `predict_proba` is a margin proxy. We
  briefly considered `GradientBoostingClassifier` but: (1) trees ignore
  embedding geometry entirely, (2) 390-dim inputs are not their happy
  place, and (3) the speed-per-row advantage of the linear probe is
  exactly the point of this module (sub-ms vs LLM grader).
- **Features**: prompt_vec (384) + 6 scalars (input_tokens, output_tokens,
  tool_call_count, latency_ms, response_length, has_thinking). Scalars
  are normalized via `StandardScaler` fit on train. Total dim = 390.
- **Label bucketing**: quality 1-2 → low (class 1), 3 → mid (class 3),
  4-5 → high (class 5). The chosen class IDs (1/3/5) match the
  fine-grained quality scale's anchor points so downstream consumers
  don't need a separate mapping table.
- **Annotation gold-labels**: rating -1 → low (1), 0 → mid (3),
  +1 → high (5). Weighted 3x in `sample_weight`.
- **Disagreement**: when both an auto_grade and an annotation exist for
  the same turn, the annotation wins (gold > silver).
- **No model trained yet**: predict() returns class=3 (mid) with
  confidence=0.0 and fallback=True. Callers can detect this and
  fall back to a slower path, or just accept the neutral prediction.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import struct
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import joblib
import numpy as np

from .. import DATA_DIR
from ..db import connect


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODELS_DIR = DATA_DIR / "models"
EMBED_DIM = 384
META_FEATURES = (
    "input_tokens",
    "output_tokens",
    "tool_call_count",
    "latency_ms",
    "response_length",
    "has_thinking",
)
FEATURE_DIM = EMBED_DIM + len(META_FEATURES)  # 384 + 6 = 390
FEATURES_VERSION = "v1-prompt384+meta6"
NEUTRAL_CLASS = 3
CLASS_LABELS = (1, 3, 5)  # low, mid, high


# ---------------------------------------------------------------------------
# Data loading + feature construction
# ---------------------------------------------------------------------------


def _unpack_embedding(blob: bytes) -> np.ndarray:
    n = len(blob) // 4
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


def _quality_to_class(quality: int | None) -> int | None:
    """Map auto_grades.quality (1-5) to coarse class id (1/3/5)."""
    if quality is None:
        return None
    if quality <= 2:
        return 1
    if quality == 3:
        return 3
    return 5


def _rating_to_class(rating: int | None) -> int | None:
    """Map annotations.rating (-1/0/+1) to coarse class id (1/3/5)."""
    if rating is None:
        return None
    if rating < 0:
        return 1
    if rating == 0:
        return 3
    return 5


def _meta_vector(row: sqlite3.Row | dict) -> np.ndarray:
    """Build the 6-dim metadata scalar block. Missing values → 0.

    `response_length` is computed from assistant_text length; `has_thinking`
    is 1 iff thinking_text is non-empty.
    """
    g = (lambda k: row[k] if k in row.keys() else None) if isinstance(row, sqlite3.Row) else row.get
    inp = float(g("input_tokens") or 0)
    out = float(g("output_tokens") or 0)
    tc = float(g("tool_call_count") or 0)
    lat = float(g("latency_ms") or 0)
    at = g("assistant_text") or ""
    rlen = float(len(at))
    tt = g("thinking_text")
    has_think = 1.0 if (tt and tt.strip()) else 0.0
    return np.array([inp, out, tc, lat, rlen, has_think], dtype=np.float32)


def _auto_grades_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='auto_grades'"
    ).fetchone()
    return row is not None


def _count_auto_grades(conn: sqlite3.Connection) -> int:
    if not _auto_grades_exists(conn):
        return 0
    return int(conn.execute("SELECT COUNT(*) FROM auto_grades").fetchone()[0])


def _count_annotations(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0])


@dataclass
class _LabeledTurn:
    turn_id: int
    label: int  # 1, 3, or 5
    weight: float
    embedding: np.ndarray  # 384-dim
    meta: np.ndarray       # 6-dim


def _load_labeled_turns(conn: sqlite3.Connection) -> list[_LabeledTurn]:
    """Build the labeled training set.

    Strategy:
    1. Pull every (turn_id, label) from auto_grades with weight 1.
    2. Pull every (turn_id, label) from annotations with weight 3 — these
       override any auto_grades label for the same turn (gold > silver).
    3. Join with prompt_vec + turn metadata. Drop turns missing prompt_vec.
    """
    by_id: dict[int, tuple[int, float]] = {}

    if _auto_grades_exists(conn):
        rows = conn.execute(
            "SELECT turn_id, quality FROM auto_grades WHERE quality IS NOT NULL"
        ).fetchall()
        for r in rows:
            cls = _quality_to_class(r["quality"])
            if cls is not None:
                by_id[r["turn_id"]] = (cls, 1.0)

    ann_rows = conn.execute(
        "SELECT turn_id, rating FROM annotations WHERE rating IS NOT NULL"
    ).fetchall()
    for r in ann_rows:
        cls = _rating_to_class(r["rating"])
        if cls is not None:
            by_id[r["turn_id"]] = (cls, 3.0)  # overrides any silver label

    if not by_id:
        return []

    placeholders = ",".join("?" * len(by_id))
    rows = conn.execute(
        f"""
        SELECT t.id, t.input_tokens, t.output_tokens, t.tool_call_count,
               t.latency_ms, t.assistant_text, t.thinking_text,
               pv.embedding
        FROM turns t
        JOIN prompt_vec pv ON pv.turn_id = t.id
        WHERE t.id IN ({placeholders})
        """,
        list(by_id.keys()),
    ).fetchall()

    out: list[_LabeledTurn] = []
    for r in rows:
        cls, w = by_id[r["id"]]
        out.append(_LabeledTurn(
            turn_id=int(r["id"]),
            label=cls,
            weight=float(w),
            embedding=_unpack_embedding(r["embedding"]),
            meta=_meta_vector(r),
        ))
    return out


def _stack(rows: list[_LabeledTurn]) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[int]]:
    """Stack labeled rows into (X_embed, X_meta, y, ids)."""
    X_embed = np.vstack([r.embedding for r in rows]).astype(np.float32)
    X_meta = np.vstack([r.meta for r in rows]).astype(np.float32)
    y = np.array([r.label for r in rows], dtype=np.int32)
    ids = [r.turn_id for r in rows]
    return X_embed, X_meta, y, ids


def _build_feature_matrix(X_embed: np.ndarray, X_meta_scaled: np.ndarray) -> np.ndarray:
    return np.hstack([X_embed, X_meta_scaled]).astype(np.float32)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _model_path(timestamp: str) -> Path:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return MODELS_DIR / f"grader-{timestamp}.joblib"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _save_model(path: Path, payload: dict) -> None:
    joblib.dump(payload, path)


def _load_model_file(path: Path) -> dict:
    return joblib.load(path)


def _record_model(
    conn: sqlite3.Connection,
    *,
    path: Path,
    trained_at: str,
    n_train: int,
    n_val: int,
    accuracy: float,
    f1_macro: float,
    features_version: str,
    notes: str | None,
) -> int:
    with conn:
        # Deactivate prior models.
        conn.execute("UPDATE classifier_models SET is_active = 0")
        cur = conn.execute(
            """
            INSERT INTO classifier_models(
                path, trained_at, n_train, n_val,
                accuracy, f1_macro, features_version, notes, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (str(path), trained_at, n_train, n_val,
             float(accuracy), float(f1_macro), features_version, notes),
        )
        return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# GraderClassifier
# ---------------------------------------------------------------------------


class GraderClassifier:
    """Local sklearn classifier exposed as `train` / `predict` / `predict_batch`."""

    def __init__(self) -> None:
        self._cached: dict | None = None
        self._cached_path: Path | None = None

    # ------ training ----------------------------------------------------

    def train(self, conn: sqlite3.Connection, *,
              model_path: Path | None = None,
              min_samples: int = 20,
              random_state: int = 42) -> dict:
        """Train a fresh classifier from the current labeled set.

        Returns a summary dict (also persisted in `classifier_models`).
        Raises `ValueError` if there isn't enough data to train.
        """
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import accuracy_score, f1_score
        from sklearn.model_selection import train_test_split
        from sklearn.preprocessing import StandardScaler

        ann_n = _count_annotations(conn)
        ag_n = _count_auto_grades(conn)
        if ag_n == 0 and ann_n < 10:
            raise ValueError(
                "Cannot train: auto_grades is empty and annotations < 10. "
                "Run `bin/grade --backfill` first (Round A), or hand-annotate "
                "at least 10 turns via the UI."
            )

        rows = _load_labeled_turns(conn)
        if len(rows) < min_samples:
            raise ValueError(
                f"Cannot train: only {len(rows)} labeled rows with prompt_vec "
                f"(need ≥ {min_samples})."
            )

        # Train on auto_grades alone? Surface a warning.
        warn_silver_only = (ann_n < 10 and ag_n > 0)

        X_embed, X_meta, y, _ids = _stack(rows)
        weights = np.array([r.weight for r in rows], dtype=np.float32)

        # Stratified split. If a class has <2 rows, stratify will choke; fall
        # back to non-stratified so the smoke path still works.
        try:
            X_e_tr, X_e_va, X_m_tr, X_m_va, y_tr, y_va, w_tr, w_va = train_test_split(
                X_embed, X_meta, y, weights,
                test_size=0.2, random_state=random_state, stratify=y,
            )
        except ValueError:
            X_e_tr, X_e_va, X_m_tr, X_m_va, y_tr, y_va, w_tr, w_va = train_test_split(
                X_embed, X_meta, y, weights,
                test_size=0.2, random_state=random_state,
            )

        scaler = StandardScaler().fit(X_m_tr)
        X_tr = _build_feature_matrix(X_e_tr, scaler.transform(X_m_tr))
        X_va = _build_feature_matrix(X_e_va, scaler.transform(X_m_va))

        # Multinomial logistic regression with L2 regularization. lbfgs is fine
        # for tens of thousands of 390-dim rows; saga would handle larger.
        # (sklearn ≥1.7 dropped the `multi_class` kwarg — it auto-detects from y.)
        clf = LogisticRegression(
            solver="lbfgs",
            max_iter=500,
            C=1.0,
            random_state=random_state,
        )
        clf.fit(X_tr, y_tr, sample_weight=w_tr)

        y_pred = clf.predict(X_va)
        acc = float(accuracy_score(y_va, y_pred, sample_weight=w_va))
        f1m = float(f1_score(y_va, y_pred, average="macro", sample_weight=w_va))

        trained_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path = model_path or _model_path(_ts())

        payload = {
            "model": clf,
            "scaler": scaler,
            "features_version": FEATURES_VERSION,
            "embed_dim": EMBED_DIM,
            "meta_features": list(META_FEATURES),
            "class_labels": list(CLASS_LABELS),
            "trained_at": trained_at,
            "n_train": int(len(y_tr)),
            "n_val": int(len(y_va)),
            "accuracy": acc,
            "f1_macro": f1m,
            "label_sources": {"auto_grades": ag_n, "annotations": ann_n},
        }
        _save_model(path, payload)

        notes = []
        if warn_silver_only:
            notes.append("trained on auto_grades only (annotations < 10)")
        notes.append(f"label_sources: auto_grades={ag_n} annotations={ann_n}")
        notes_str = "; ".join(notes)

        model_id = _record_model(
            conn,
            path=path,
            trained_at=trained_at,
            n_train=len(y_tr),
            n_val=len(y_va),
            accuracy=acc,
            f1_macro=f1m,
            features_version=FEATURES_VERSION,
            notes=notes_str,
        )

        return {
            "model_id": model_id,
            "n_train": int(len(y_tr)),
            "n_val": int(len(y_va)),
            "accuracy": acc,
            "f1": f1m,
            "model_path": str(path),
            "silver_only": warn_silver_only,
            "features_version": FEATURES_VERSION,
        }

    # ------ inference ---------------------------------------------------

    def _load_active(self, conn: sqlite3.Connection) -> tuple[dict, int] | None:
        row = conn.execute(
            """
            SELECT id, path FROM classifier_models
            WHERE is_active = 1
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if not row:
            # No active model. Try the most recent by id as a soft fallback —
            # there might be one whose `is_active` got zeroed during a retrain
            # that failed mid-flight.
            row = conn.execute(
                "SELECT id, path FROM classifier_models ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
        path = Path(row["path"])
        if not path.exists():
            return None
        if self._cached and self._cached_path == path:
            return self._cached, int(row["id"])
        payload = _load_model_file(path)
        self._cached = payload
        self._cached_path = path
        return payload, int(row["id"])

    def _featurize_turn(self, conn: sqlite3.Connection, turn_id: int) -> np.ndarray | None:
        row = conn.execute(
            """
            SELECT t.input_tokens, t.output_tokens, t.tool_call_count,
                   t.latency_ms, t.assistant_text, t.thinking_text,
                   pv.embedding
            FROM turns t
            JOIN prompt_vec pv ON pv.turn_id = t.id
            WHERE t.id = ?
            """,
            (turn_id,),
        ).fetchone()
        if not row:
            return None
        embed = _unpack_embedding(row["embedding"])
        meta = _meta_vector(row)
        return np.concatenate([embed, meta]).astype(np.float32)

    def predict(self, conn: sqlite3.Connection, turn_id: int) -> dict:
        active = self._load_active(conn)
        if active is None:
            return {"quality": NEUTRAL_CLASS, "confidence": 0.0, "fallback": True,
                    "turn_id": turn_id, "model_id": None}
        payload, model_id = active
        feat = self._featurize_turn(conn, turn_id)
        if feat is None:
            return {"quality": NEUTRAL_CLASS, "confidence": 0.0, "fallback": True,
                    "turn_id": turn_id, "model_id": model_id,
                    "reason": "missing prompt_vec or turn"}
        # Build X manually so we share the same scaler.
        embed = feat[:EMBED_DIM][None, :]
        meta_scaled = payload["scaler"].transform(feat[EMBED_DIM:][None, :])
        X = np.hstack([embed, meta_scaled]).astype(np.float32)
        proba = payload["model"].predict_proba(X)[0]
        idx = int(np.argmax(proba))
        classes = list(payload["model"].classes_)
        quality = int(classes[idx])
        conf = float(proba[idx])
        return {
            "quality": quality,
            "confidence": conf,
            "fallback": False,
            "turn_id": turn_id,
            "model_id": model_id,
        }

    def predict_batch(self, conn: sqlite3.Connection, turn_ids: list[int]) -> list[dict]:
        active = self._load_active(conn)
        if active is None:
            return [
                {"quality": NEUTRAL_CLASS, "confidence": 0.0, "fallback": True,
                 "turn_id": tid, "model_id": None}
                for tid in turn_ids
            ]
        payload, model_id = active

        if not turn_ids:
            return []

        placeholders = ",".join("?" * len(turn_ids))
        rows = conn.execute(
            f"""
            SELECT t.id, t.input_tokens, t.output_tokens, t.tool_call_count,
                   t.latency_ms, t.assistant_text, t.thinking_text,
                   pv.embedding
            FROM turns t
            JOIN prompt_vec pv ON pv.turn_id = t.id
            WHERE t.id IN ({placeholders})
            """,
            list(turn_ids),
        ).fetchall()
        by_id = {int(r["id"]): r for r in rows}

        results: list[dict] = []
        present: list[tuple[int, np.ndarray, np.ndarray]] = []
        for tid in turn_ids:
            r = by_id.get(tid)
            if r is None:
                results.append({"quality": NEUTRAL_CLASS, "confidence": 0.0,
                                "fallback": True, "turn_id": tid,
                                "model_id": model_id,
                                "reason": "missing prompt_vec or turn"})
            else:
                present.append((tid, _unpack_embedding(r["embedding"]), _meta_vector(r)))

        if present:
            X_embed = np.vstack([p[1] for p in present]).astype(np.float32)
            X_meta = np.vstack([p[2] for p in present]).astype(np.float32)
            X_meta_scaled = payload["scaler"].transform(X_meta)
            X = np.hstack([X_embed, X_meta_scaled]).astype(np.float32)
            probas = payload["model"].predict_proba(X)
            classes = list(payload["model"].classes_)
            for (tid, _emb, _meta), proba in zip(present, probas):
                idx = int(np.argmax(proba))
                results.append({
                    "quality": int(classes[idx]),
                    "confidence": float(proba[idx]),
                    "fallback": False,
                    "turn_id": tid,
                    "model_id": model_id,
                })

        # Preserve input order.
        by_tid = {r["turn_id"]: r for r in results}
        return [by_tid[tid] for tid in turn_ids]

    # ------ metadata ----------------------------------------------------

    def latest_model_meta(self, conn: sqlite3.Connection | None = None) -> dict | None:
        own_conn = conn is None
        if own_conn:
            conn = connect()
        try:
            row = conn.execute(
                """
                SELECT id, path, trained_at, n_train, n_val,
                       accuracy, f1_macro, features_version, notes, is_active
                FROM classifier_models
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            n_pred = conn.execute(
                "SELECT COUNT(*) AS n FROM classifier_predictions WHERE model_id = ?",
                (d["id"],),
            ).fetchone()["n"]
            d["n_predictions"] = int(n_pred)
            return d
        finally:
            if own_conn:
                conn.close()


# ---------------------------------------------------------------------------
# Prediction persistence (predict-all)
# ---------------------------------------------------------------------------


def _store_predictions(conn: sqlite3.Connection, preds: Iterable[dict],
                       model_id: int) -> int:
    n = 0
    with conn:
        for p in preds:
            if p.get("fallback"):
                continue
            conn.execute(
                """
                INSERT INTO classifier_predictions(turn_id, quality, confidence,
                                                   model_id, predicted_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(turn_id) DO UPDATE SET
                    quality      = excluded.quality,
                    confidence   = excluded.confidence,
                    model_id     = excluded.model_id,
                    predicted_at = excluded.predicted_at
                """,
                (p["turn_id"], int(p["quality"]), float(p["confidence"]), model_id),
            )
            n += 1
    return n


# ---------------------------------------------------------------------------
# Retrain-trigger policy
# ---------------------------------------------------------------------------


def retrain_recommendation(conn: sqlite3.Connection) -> dict:
    """Return a dict describing whether a retrain is recommended."""
    meta = GraderClassifier().latest_model_meta(conn)
    n_ann = _count_annotations(conn)
    if meta is None:
        return {"recommended": True, "reason": "no model trained yet",
                "current_annotations": n_ann, "last_train_n": None}
    last_n = int(meta.get("n_train") or 0)
    # Heuristic: when annotations have grown by ≥ 20 since the last train,
    # recommend retraining. We can't perfectly back out how many of the
    # last train's n_train came from annotations vs auto_grades — use the
    # raw annotations count as a proxy (it monotonically grows).
    threshold = last_n + 20
    if n_ann >= threshold and n_ann > 0:
        return {"recommended": True,
                "reason": f"annotations ({n_ann}) exceed last_train.n_train + 20 ({threshold})",
                "current_annotations": n_ann, "last_train_n": last_n}
    return {"recommended": False, "current_annotations": n_ann, "last_train_n": last_n}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_train(args) -> int:
    conn = connect()
    try:
        result = GraderClassifier().train(conn, min_samples=args.min_samples)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _cmd_predict(args) -> int:
    conn = connect()
    out = GraderClassifier().predict(conn, args.turn_id)
    print(json.dumps(out, indent=2))
    return 0


def _cmd_predict_all(args) -> int:
    conn = connect()
    meta = GraderClassifier().latest_model_meta(conn)
    if meta is None:
        print("error: no trained model — run `bin/train-classifier` first",
              file=sys.stderr)
        return 1
    model_id = int(meta["id"])
    where = ""
    params: list = []
    if args.unscored_only:
        where = """
            WHERE NOT EXISTS (
              SELECT 1 FROM classifier_predictions p WHERE p.turn_id = t.id
            )
        """
    sql = f"""
        SELECT t.id FROM turns t
        JOIN prompt_vec pv ON pv.turn_id = t.id
        {where}
        ORDER BY t.id ASC
        {"LIMIT ?" if args.limit else ""}
    """
    if args.limit:
        params.append(args.limit)
    ids = [int(r["id"]) for r in conn.execute(sql, params).fetchall()]
    if not ids:
        print("no turns to predict")
        return 0

    started = time.monotonic()
    batch = 256
    classifier = GraderClassifier()
    total_stored = 0
    printed = 0
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        preds = classifier.predict_batch(conn, chunk)
        total_stored += _store_predictions(conn, preds, model_id)
        if args.limit and printed < min(5, len(ids)):
            for p in preds[: max(0, 5 - printed)]:
                print(json.dumps({"turn_id": p["turn_id"],
                                  "quality": p["quality"],
                                  "confidence": round(p["confidence"], 3),
                                  "model_id": p["model_id"]}))
                printed += 1

    dt = time.monotonic() - started
    print(f"\nstored {total_stored} predictions in {dt:.2f}s "
          f"({total_stored / max(dt, 1e-9):.0f} rows/s) "
          f"using model_id={model_id}")
    return 0


def _cmd_eval(args) -> int:
    """Re-evaluate the active model against the *current* labeled set."""
    from sklearn.metrics import accuracy_score, f1_score, classification_report

    conn = connect()
    classifier = GraderClassifier()
    if args.model:
        payload = _load_model_file(Path(args.model))
        model_id = None
    else:
        active = classifier._load_active(conn)
        if active is None:
            print("error: no trained model", file=sys.stderr)
            return 1
        payload, model_id = active

    rows = _load_labeled_turns(conn)
    if not rows:
        print("error: no labeled rows to eval", file=sys.stderr)
        return 1

    X_embed, X_meta, y, _ids = _stack(rows)
    X_meta_scaled = payload["scaler"].transform(X_meta)
    X = np.hstack([X_embed, X_meta_scaled]).astype(np.float32)
    y_pred = payload["model"].predict(X)

    # Split agreement by source.
    src = {}  # turn_id -> "annotation" | "auto_grade"
    ann_ids = {r["turn_id"] for r in conn.execute(
        "SELECT turn_id FROM annotations WHERE rating IS NOT NULL"
    ).fetchall()}
    for r in rows:
        src[r.turn_id] = "annotation" if r.turn_id in ann_ids else "auto_grade"

    ann_mask = np.array([src[r.turn_id] == "annotation" for r in rows])
    ag_mask = ~ann_mask

    out = {
        "model_id": model_id,
        "n_total": int(len(rows)),
        "accuracy_total": float(accuracy_score(y, y_pred)),
        "f1_macro_total": float(f1_score(y, y_pred, average="macro")),
        "agreement_with_annotations": {
            "n": int(ann_mask.sum()),
            "accuracy": float(accuracy_score(y[ann_mask], y_pred[ann_mask])) if ann_mask.any() else None,
        },
        "agreement_with_auto_grades": {
            "n": int(ag_mask.sum()),
            "accuracy": float(accuracy_score(y[ag_mask], y_pred[ag_mask])) if ag_mask.any() else None,
        },
    }
    print(json.dumps(out, indent=2))
    print("\nper-class report (gold + silver mixed):")
    print(classification_report(y, y_pred, zero_division=0))
    return 0


def _cmd_status(args) -> int:
    conn = connect()
    classifier = GraderClassifier()
    meta = classifier.latest_model_meta(conn)
    ag_n = _count_auto_grades(conn)
    ann_n = _count_annotations(conn)
    retrain = retrain_recommendation(conn)
    n_pred = int(conn.execute("SELECT COUNT(*) FROM classifier_predictions").fetchone()[0])
    agree = None
    if meta and ag_n > 0:
        agree_row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN
                  (ag.quality <= 2 AND p.quality = 1) OR
                  (ag.quality = 3 AND p.quality = 3) OR
                  (ag.quality >= 4 AND p.quality = 5)
                THEN 1 ELSE 0 END) AS hits,
              COUNT(*) AS n
            FROM classifier_predictions p
            JOIN auto_grades ag ON ag.turn_id = p.turn_id
            """
        ).fetchone()
        if agree_row and agree_row["n"]:
            agree = {"hits": int(agree_row["hits"] or 0), "n": int(agree_row["n"])}

    out = {
        "model": meta,
        "label_counts": {"auto_grades": ag_n, "annotations": ann_n},
        "n_predictions": n_pred,
        "retrain": retrain,
        "agreement_with_auto_grades": agree,
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="prompt_telemetry.ai.classifier",
        description="Local grader classifier (sklearn).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pt = sub.add_parser("train", help="Train a fresh model from labeled data.")
    pt.add_argument("--min-samples", type=int, default=20,
                    help="Minimum labeled rows required to train (default 20).")
    pt.set_defaults(fn=_cmd_train)

    pp = sub.add_parser("predict", help="Predict quality for a single turn.")
    pp.add_argument("turn_id", type=int)
    pp.set_defaults(fn=_cmd_predict)

    pa = sub.add_parser("predict-all", help="Predict for many turns; persist to classifier_predictions.")
    pa.add_argument("--unscored-only", action="store_true",
                    help="Only score turns without an existing prediction.")
    pa.add_argument("--limit", type=int, default=None,
                    help="Cap the number of turns scored.")
    pa.set_defaults(fn=_cmd_predict_all)

    pe = sub.add_parser("eval", help="Re-evaluate the active model on the current labeled set.")
    pe.add_argument("--model", default=None,
                    help="Path to a specific joblib model (default: active).")
    pe.set_defaults(fn=_cmd_eval)

    ps = sub.add_parser("status", help="Print latest model metadata + counts.")
    ps.set_defaults(fn=_cmd_status)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
