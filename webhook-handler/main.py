"""LPG webhook handler — Layer 3.5: structured logging.

Receives Shift4Shop Order New webhooks at /webhooks/shift4/order-created.
Validates the payload with Pydantic, ingests into shift4.* tables via
ingest_order, returns a JSON summary.

Status filtering per ADR-0009: only OrderStatusID values in
ORDER_STATUS_MAP are ingested. Anything else (including OrderStatusID
21 / Quote) is acknowledged with 200 and logged as a skip.

Logging: structured JSON in production (Cloud Run), colored pretty
output for local dev. Every request gets a request_id bound to the
log context.
"""

from __future__ import annotations

import time
import uuid

import structlog
from fastapi import FastAPI, Request

from ingest import ingest_order
from logging_config import configure_logging
from shift4_models import ORDER_STATUS_MAP, Shift4OrderPayload

configure_logging()
log = structlog.get_logger()

app = FastAPI(title="lpg-webhook-handler", version="0.3.0")


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """Bind a request_id to the log context for the duration of the
    request, and log start/end events with status + duration."""
    request_id = str(uuid.uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )
    log.info("request_started")
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        log.error("request_failed", duration_ms=duration_ms, exc_info=True)
        raise
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    log.info(
        "request_finished",
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/")
def root():
    """Root endpoint — quick alive check."""
    return {"service": "lpg-webhook-handler", "status": "ok"}


@app.get("/healthz")
def healthz():
    """Health check endpoint for Cloud Run liveness probes."""
    return {"status": "ok"}


@app.post("/webhooks/shift4/order-created")
def shift4_order_created(payload: Shift4OrderPayload):
    """Receive a Shift4 'Order New' webhook.

    Validates the payload against Shift4OrderPayload. FastAPI returns
    422 automatically if validation fails. Otherwise, classifies the
    order by status and either ingests it (statuses in ORDER_STATUS_MAP
    excluding Quote) or skips it (statuses out of allow-list, or Quote).
    """
    structlog.contextvars.bind_contextvars(
        order_id=payload.shift4_order_id,
        order_status_id=payload.order_status_id,
    )

    status_id = payload.order_status_id
    status_text = ORDER_STATUS_MAP.get(status_id)

    if status_text is None:
        log.warning(
            "order_skipped_unknown_status",
            reason="order_status_id not in allow-list",
        )
        return {
            "received": True,
            "ingested": False,
            "reason": f"order_status_id={status_id} not in allow-list",
            "order_id": payload.shift4_order_id,
        }

    if status_text == "Quote":
        log.info(
            "order_skipped_quote",
            reason="quote status excluded by business rule",
        )
        return {
            "received": True,
            "ingested": False,
            "reason": "quote status excluded by business rule",
            "order_id": payload.shift4_order_id,
        }

    # Status is one of New, Processing, Shipped. Persist to DB.
    log.info("order_ingest_starting", status=status_text)
    try:
        result = ingest_order(payload)
    except Exception:
        log.error("order_ingest_failed", exc_info=True)
        raise

    log.info(
        "order_ingested",
        status=status_text,
        items=result["items_inserted"],
        shipments=result["shipments_inserted"],
    )

    return {
        "received": True,
        "ingested": True,
        **result,
    }
    
