"""OTLP/HTTP receiver for dash0-agent-plugin. JSON only in A.2; protobuf returns 415.

Endpoints follow the OTLP/HTTP spec under the OTLP_URL base:
  POST /v1/traces   -> ExportTraceServiceRequest    (JSON encoding)
  POST /v1/logs     -> ExportLogsServiceRequest
  POST /v1/metrics  -> accepted-and-dropped (reserved)

Insertion happens in a BackgroundTask so the hook subprocess sees a fast 200 ack.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response

from ..db import connect
from .store import insert_log_export, insert_trace_export

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dash0"])


def _decode_json_or_415(body: bytes, content_type: str | None) -> dict[str, Any]:
    ct = (content_type or "application/json").split(";")[0].strip().lower()
    if ct == "application/json":
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
    if ct == "application/x-protobuf":
        raise HTTPException(
            status_code=415,
            detail="application/x-protobuf not supported; set dash0 to send JSON or install opentelemetry-proto",
        )
    raise HTTPException(status_code=415, detail=f"unsupported content-type: {ct}")


@router.post("/v1/traces")
async def post_traces(
    request: Request,
    background: BackgroundTasks,
    content_type: str | None = Header(default=None, alias="content-type"),
) -> Response:
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    payload = _decode_json_or_415(body, content_type)
    background.add_task(_insert_traces_safe, payload, body)
    return Response(
        content=json.dumps({"partialSuccess": {"rejectedSpans": "0"}}),
        media_type="application/json",
    )


@router.post("/v1/logs")
async def post_logs(
    request: Request,
    background: BackgroundTasks,
    content_type: str | None = Header(default=None, alias="content-type"),
) -> Response:
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    payload = _decode_json_or_415(body, content_type)
    background.add_task(_insert_logs_safe, payload)
    return Response(
        content=json.dumps({"partialSuccess": {"rejectedLogRecords": "0"}}),
        media_type="application/json",
    )


@router.post("/v1/metrics")
async def post_metrics(request: Request) -> Response:
    await request.body()
    return Response(
        content=json.dumps({"partialSuccess": {"rejectedDataPoints": "0"}}),
        media_type="application/json",
    )


@router.get("/dash0/health")
def dash0_health() -> dict[str, Any]:
    conn = connect()
    row = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM dash0_spans)               AS spans_total,
          (SELECT COUNT(*) FROM dash0_log_records)         AS logs_total,
          (SELECT COUNT(*) FROM dash0_span_events)         AS span_events_total,
          (SELECT COUNT(*) FROM dash0_resources)           AS resources_total,
          (SELECT MAX(received_at) FROM dash0_spans)       AS last_span_received_at,
          (SELECT MAX(received_at) FROM dash0_log_records) AS last_log_received_at
        """
    ).fetchone()
    return {"ok": True, **dict(row)}


def _insert_traces_safe(payload: dict[str, Any], raw_body: bytes) -> None:
    try:
        conn = connect()
        with conn:
            insert_trace_export(conn, payload, raw_body=raw_body)
    except Exception:
        logger.exception("dash0 trace insert failed")


def _insert_logs_safe(payload: dict[str, Any]) -> None:
    try:
        conn = connect()
        with conn:
            insert_log_export(conn, payload)
    except Exception:
        logger.exception("dash0 log insert failed")
