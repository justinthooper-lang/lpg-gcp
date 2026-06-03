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
from fastapi.responses import HTMLResponse, JSONResponse
from pg8000.exceptions import DatabaseError, InterfaceError
from pydantic import ValidationError
from auth import verify_token
from db import get_connection
from ingest import ingest_order
from logging_config import configure_logging
from shift4_models import ORDER_STATUS_MAP, Shift4OrderPayload

import json

configure_logging()
log = structlog.get_logger()

app = FastAPI(title="lpg-webhook-handler", version="0.6.0")


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
    
    return {"status": "ready", "method": "GET", "expects": "POST"}
@app.post("/webhooks/shift4/order-created")
async def shift4_order_created(request: Request):
    """Receive a Shift4 'Order New' webhook.

    Verifies HMAC signature, parses the body as a Shift4OrderPayload,
    classifies by status, and either ingests it or returns a skip
    response. Returns 401 if the signature is missing/invalid, 422
    if the body fails validation, 503 if the DB is unreachable, 500
    if a DB query fails.
    """
    body = await request.body()
    received_token = request.query_params.get("token")

    if not verify_token(received_token):
        return JSONResponse(
            status_code=401,
            content={
                "received": True,
                "ingested": False,
                "reason": "invalid or missing webhook token",
            },
        )

    # Shift4 sends order webhooks as a JSON array containing a single
    # order object: [{...}]. We unwrap before validating.
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        log.warning("webhook_invalid_json", error=str(exc))
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid JSON: {exc}"},
        )

    if isinstance(parsed, list):
        if len(parsed) != 1:
            log.warning("webhook_unexpected_array_size", size=len(parsed))
            return JSONResponse(
                status_code=422,
                content={"detail": f"expected array of 1 order, got {len(parsed)}"},
            )
        order_dict = parsed[0]
    elif isinstance(parsed, dict):
        # Accept bare-object form too (in case Shift4 changes behavior
        # or for tests).
        order_dict = parsed
    else:
        return JSONResponse(
            status_code=422,
            content={"detail": "expected JSON object or array of one object"},
        )

    try:
        payload = Shift4OrderPayload.model_validate(order_dict)
    except ValidationError as exc:
        log.warning("webhook_validation_failed", errors=exc.errors())
        return JSONResponse(status_code=422, content={"detail": exc.errors()})
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
    except InterfaceError:
        log.error("order_ingest_db_unavailable", exc_info=True)
        return JSONResponse(
            status_code=503,
            content={
                "received": True,
                "ingested": False,
                "reason": "database unavailable; retry later",
                "order_id": payload.shift4_order_id,
            },
        )
    except DatabaseError:
        log.error("order_ingest_db_error", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "received": True,
                "ingested": False,
                "reason": "database error; investigation required",
                "order_id": payload.shift4_order_id,
            },
        )
    except Exception:
        log.error("order_ingest_unexpected_error", exc_info=True)
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
@app.get("/orders")
async def list_orders(request: Request):
    """List recent orders (authenticated read endpoint).

    Query params:
      token: required, must match SHIFT4_WEBHOOK_TOKEN
      limit: optional, 1-100, default 20

    Returns a JSON array of recent orders, newest first. Project only
    a safe subset of columns — no raw_payload (contains PII).
    """
    received_token = request.query_params.get("token")
    if not verify_token(received_token):
        return JSONResponse(
            status_code=401,
            content={"error": "invalid or missing token"},
        )

    # Parse and clamp limit.
    try:
        limit = int(request.query_params.get("limit", "20"))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 100))

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT
                        shift4_order_id,
                        invoice_number,
                        bill_first_name || ' ' || bill_last_name AS customer_name,
                        bill_email,
                        order_status,
                        grand_total,
                        updated_at
                    FROM shift4.orders
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
            finally:
                cur.close()
    except Exception as exc:
        log.error("orders_list_db_error", error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"error": "database error"},
        )

    orders = [
        {
            "shift4_order_id": row[0],
            "invoice_number": row[1],
            "customer_name": row[2],
            "email": row[3],
            "status": row[4],
            "grand_total": str(row[5]),
            "updated_at": row[6].isoformat() if row[6] else None,
        }
        for row in rows
    ]

    return {"count": len(orders), "orders": orders}

@app.get("/orders.html", response_class=HTMLResponse)
async def list_orders_html(request: Request):
    """HTML view of recent orders. Same auth/data as /orders."""
    # Reuse the JSON endpoint's logic by calling it.
    response_data = await list_orders(request)

    # If list_orders returned a JSONResponse (auth/error), pass it through.
    if isinstance(response_data, JSONResponse):
        return response_data

    orders = response_data["orders"]

    rows_html = "".join(
        f"""
        <tr>
            <td>{o['shift4_order_id']}</td>
            <td>{o['invoice_number'] or ''}</td>
            <td>{o['customer_name']}</td>
            <td>{o['email'] or ''}</td>
            <td>{o['status']}</td>
            <td style="text-align:right">${o['grand_total']}</td>
            <td>{o['updated_at']}</td>
        </tr>
        """
        for o in orders
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>LPG — Recent Orders</title>
    <style>
        body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 1100px; margin: 2em auto; padding: 0 1em; }}
        h1 {{ font-weight: 500; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ padding: 8px 12px; border-bottom: 1px solid #e0e0e0; text-align: left; }}
        th {{ background: #f5f5f5; font-weight: 500; }}
        tr:hover {{ background: #fafafa; }}
        .meta {{ color: #888; font-size: 0.9em; }}
    </style>
</head>
<body>
    <h1>Recent orders</h1>
    <p class="meta">{response_data['count']} order{'s' if response_data['count'] != 1 else ''}</p>
    <table>
        <thead>
            <tr>
                <th>Order ID</th>
                <th>Invoice</th>
                <th>Customer</th>
                <th>Email</th>
                <th>Status</th>
                <th style="text-align:right">Total</th>
                <th>Updated</th>
            </tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
    </table>
</body>
</html>"""

    return HTMLResponse(content=html)

@app.get("/webhooks/shift4/order-created")
async def shift4_order_created_probe():
    """Respond 200 to Shift4's pre-POST GET probe."""
    return {"status": "ready", "method": "GET", "expects": "POST"} 