"""Web Push notification delivery channel.

Replaces the older `osascript` macOS-only notify path with a real Web Push
pipeline. The dashboard's service worker registers against this app's VAPID
keypair (generated lazily, stored in `data/vapid_{private,public}.pem`).
Once one or more subscriptions are registered in `push_subscriptions`, any
caller can `notify(conn, title, body, url=...)` to fan a payload out to every
active browser. The digest module (round C) is the primary caller.

Failure modes are confined: a broken keypair, an unreachable push endpoint, a
missing library — none of them are allowed to take down the capture pipeline
or the UI. Every external call is wrapped, and we degrade to a logged no-op.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import stat
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import DATA_DIR

VAPID_PRIVATE_PATH = DATA_DIR / "vapid_private.pem"
VAPID_PUBLIC_PATH = DATA_DIR / "vapid_public.pem"
PUSH_LOG_PATH = DATA_DIR / "push.log"

# RFC 8292: the subject must be an mailto: or https: URL identifying the app
# server. For a local-only app there is no real address; a placeholder
# loopback URL is fine and is what most browsers accept.
VAPID_CLAIMS_SUBJECT = "mailto:telemetrify@localhost"

# Status codes from the push service that mean the subscription is dead and
# should be evicted from the DB rather than retried.
_DEAD_STATUS_CODES = {404, 410}


# ─── logging ──────────────────────────────────────────────────────────────


def _log(line: str) -> None:
    """Best-effort append to `data/push.log`. Never raises."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with PUSH_LOG_PATH.open("a", encoding="utf-8") as fp:
            fp.write(f"{ts} {line}\n")
    except Exception:
        # logging must not break delivery
        pass


# ─── VAPID keys ───────────────────────────────────────────────────────────


def ensure_vapid_keys() -> None:
    """Generate `data/vapid_{private,public}.pem` on first use. Idempotent.

    The private key is chmod 600 — only the owning user can read it.
    """
    if VAPID_PRIVATE_PATH.exists() and VAPID_PUBLIC_PATH.exists():
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        from py_vapid import Vapid

        v = Vapid()
        v.generate_keys()
        v.save_key(str(VAPID_PRIVATE_PATH))
        v.save_public_key(str(VAPID_PUBLIC_PATH))
        # tighten private-key perms — owner read/write only
        try:
            os.chmod(VAPID_PRIVATE_PATH, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        _log(f"ensure_vapid_keys: generated keypair at {VAPID_PRIVATE_PATH.name}")
    except Exception:
        _log(f"ensure_vapid_keys: FAILED\n{traceback.format_exc()}")


def _load_vapid() -> Any | None:
    """Load a `Vapid` object from disk, generating keys if missing.

    Returns None on any failure so callers can degrade gracefully.
    """
    try:
        ensure_vapid_keys()
        from py_vapid import Vapid

        return Vapid.from_file(str(VAPID_PRIVATE_PATH))
    except Exception:
        _log(f"_load_vapid: FAILED\n{traceback.format_exc()}")
        return None


def vapid_public_key() -> str:
    """Return the URL-safe base64 of the raw uncompressed public key bytes.

    This is the `applicationServerKey` value the page hands to
    `pushManager.subscribe`. Returns "" on failure (caller can show an error).
    """
    try:
        v = _load_vapid()
        if v is None:
            return ""
        from cryptography.hazmat.primitives import serialization

        raw = v.public_key.public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    except Exception:
        _log(f"vapid_public_key: FAILED\n{traceback.format_exc()}")
        return ""


# ─── subscription storage ────────────────────────────────────────────────


def register_subscription(
    conn: sqlite3.Connection,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None,
) -> int:
    """Upsert a push subscription. Returns the row id.

    Uniqueness is on `endpoint`; re-subscribing the same browser refreshes
    `p256dh`/`auth`/`user_agent` and bumps `last_used_at` to now.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions(endpoint, p256dh, auth, user_agent, last_used_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh       = excluded.p256dh,
                auth         = excluded.auth,
                user_agent   = excluded.user_agent,
                last_used_at = datetime('now')
            """,
            (endpoint, p256dh, auth, user_agent),
        )
    row = conn.execute(
        "SELECT id FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
    ).fetchone()
    sub_id = int(row[0]) if row else -1
    _log(f"register_subscription: id={sub_id} endpoint={_short(endpoint)} ua={(user_agent or '')[:60]!r}")
    return sub_id


def unregister_subscription(conn: sqlite3.Connection, endpoint: str) -> bool:
    """Remove a subscription by endpoint. Returns True if a row was removed."""
    with conn:
        cur = conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
        )
        removed = cur.rowcount > 0
    _log(f"unregister_subscription: endpoint={_short(endpoint)} removed={removed}")
    return removed


def _short(endpoint: str) -> str:
    """Compact representation of an endpoint URL for logs (no full token)."""
    if not endpoint:
        return "(empty)"
    if len(endpoint) <= 80:
        return endpoint
    return endpoint[:60] + "…" + endpoint[-12:]


# ─── send ────────────────────────────────────────────────────────────────


def _all_subscriptions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, endpoint, p256dh, auth FROM push_subscriptions"
    ).fetchall()


def _touch_last_used(conn: sqlite3.Connection, sub_id: int) -> None:
    try:
        with conn:
            conn.execute(
                "UPDATE push_subscriptions SET last_used_at = datetime('now') WHERE id = ?",
                (sub_id,),
            )
    except Exception:
        pass


def _delete_by_id(conn: sqlite3.Connection, sub_id: int) -> None:
    try:
        with conn:
            conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,))
    except Exception:
        pass


def notify(
    conn: sqlite3.Connection,
    title: str,
    body: str,
    url: str | None = None,
    *,
    dry_run: bool = False,
) -> dict:
    """Fan out a Web Push to every active subscription.

    Returns `{sent, failed, removed}`. On a 404 / 410 from the push service
    the subscription is evicted from the DB (the browser revoked it).

    `dry_run=True` writes log entries but never touches the network.
    """
    result = {"sent": 0, "failed": 0, "removed": 0}

    try:
        subs = _all_subscriptions(conn)
    except Exception:
        _log(f"notify: subscription read FAILED\n{traceback.format_exc()}")
        return result

    if not subs:
        _log(
            f"notify: no subscriptions (title={title!r}, body={body!r}, "
            f"url={url!r}, dry_run={dry_run})"
        )
        return result

    payload = json.dumps(
        {"title": title, "body": body, "url": url or "/dashboard"},
        ensure_ascii=False,
    )

    if dry_run:
        for sub in subs:
            _log(f"notify[dry]: would send to id={sub['id']} endpoint={_short(sub['endpoint'])}")
        # In dry_run mode we still report 0 actually-sent — semantics match
        # the spec's verification: `bin/notify --dry-run` prints all zeros
        # when there are no real network attempts.
        return result

    v = _load_vapid()
    if v is None:
        _log(f"notify: VAPID load failed, aborting send to {len(subs)} sub(s)")
        result["failed"] = len(subs)
        return result

    try:
        from pywebpush import WebPushException, webpush
    except Exception:
        _log(f"notify: pywebpush import FAILED\n{traceback.format_exc()}")
        result["failed"] = len(subs)
        return result

    # pywebpush.webpush takes `vapid_private_key` as either a Vapid object,
    # a base64 DER/raw string, OR a path to a PEM file. We pass the path —
    # cleanest, and pywebpush already special-cases `os.path.isfile(...)`.
    if not VAPID_PRIVATE_PATH.exists():
        _log(f"notify: VAPID private key missing at {VAPID_PRIVATE_PATH}")
        result["failed"] = len(subs)
        return result

    priv_key_path = str(VAPID_PRIVATE_PATH)
    claims = {"sub": VAPID_CLAIMS_SUBJECT}

    for sub in subs:
        sub_info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        }
        try:
            webpush(
                subscription_info=sub_info,
                data=payload,
                vapid_private_key=priv_key_path,
                vapid_claims=dict(claims),
                timeout=10,
            )
            result["sent"] += 1
            _touch_last_used(conn, sub["id"])
            _log(f"notify: sent id={sub['id']} endpoint={_short(sub['endpoint'])}")
        except WebPushException as exc:  # network/status error
            status = None
            resp = getattr(exc, "response", None)
            if resp is not None:
                status = getattr(resp, "status_code", None)
            if status in _DEAD_STATUS_CODES:
                _delete_by_id(conn, sub["id"])
                result["removed"] += 1
                _log(
                    f"notify: removed dead id={sub['id']} status={status} "
                    f"endpoint={_short(sub['endpoint'])}"
                )
            else:
                result["failed"] += 1
                _log(
                    f"notify: FAILED id={sub['id']} status={status} "
                    f"endpoint={_short(sub['endpoint'])} err={exc!r}"
                )
        except Exception:
            result["failed"] += 1
            _log(
                f"notify: FAILED id={sub['id']} endpoint={_short(sub['endpoint'])}\n"
                f"{traceback.format_exc()}"
            )

    _log(
        f"notify: title={title!r} sent={result['sent']} "
        f"failed={result['failed']} removed={result['removed']}"
    )
    return result
