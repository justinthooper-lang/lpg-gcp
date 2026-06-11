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
from auth import verify_token, is_authorized_read, is_admin_service
from lpg_common.db import get_connection
from ingest import ingest_order
from logging_config import configure_logging
from shift4_models import ORDER_STATUS_MAP, Shift4OrderPayload
from decimal import Decimal
from purchase_order_builder import Fee
from purchase_order_repository import (
    PurchaseOrderError,
    generate_purchase_order,
    purchase_order_to_dict,
)

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
async def list_orders(request: Request):
    """List recent orders (authenticated read endpoint).

    Query params:
      token: required, must match SHIFT4_WEBHOOK_TOKEN
      limit: optional, 1-100, default 20

    Returns a JSON array of recent orders, newest first. Project only
    a safe subset of columns — no raw_payload (contains PII).
    """
    received_token = request.query_params.get("token")
    if not is_authorized_read(received_token):
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
            <td><a href="/orders/{o['shift4_order_id']}.html">{o['shift4_order_id']}</a></td>
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

async def get_order(order_id: int, request: Request):
    """Detail view of a single order, with line items and shipments."""
    received_token = request.query_params.get("token")
    if not is_authorized_read(received_token):
        return JSONResponse(
            status_code=401,
            content={"error": "invalid or missing token"},
        )

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT
                        shift4_order_id, shift4_customer_id, invoice_number,
                        order_date, order_status, comments,
                        bill_first_name, bill_last_name, bill_company,
                        bill_address, bill_address2, bill_city, bill_state,
                        bill_zip, bill_country, bill_phone, bill_email,
                        subtotal, tax, shipping_cost, discount, grand_total,
                        updated_at
                    FROM shift4.orders
                    WHERE shift4_order_id = %s
                    """,
                    (order_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        status_code=404,
                        content={"error": f"order {order_id} not found"},
                    )

                order = {
                    "shift4_order_id": row[0],
                    "shift4_customer_id": row[1],
                    "invoice_number": row[2],
                    "order_date": row[3].isoformat() if row[3] else None,
                    "order_status": row[4],
                    "comments": row[5],
                    "billing": {
                        "first_name": row[6],
                        "last_name": row[7],
                        "company": row[8],
                        "address": row[9],
                        "address2": row[10],
                        "city": row[11],
                        "state": row[12],
                        "zip": row[13],
                        "country": row[14],
                        "phone": row[15],
                        "email": row[16],
                    },
                    "totals": {
                        "subtotal": str(row[17]),
                        "tax": str(row[18]),
                        "shipping_cost": str(row[19]),
                        "discount": str(row[20]),
                        "grand_total": str(row[21]),
                    },
                    "updated_at": row[22].isoformat() if row[22] else None,
                }

                cur.execute(
                    """
                    SELECT
                        oi.id,
                        oi.sku,
                        oi.quantity,
                        oi.unit_price,
                        oi.item_unit_cost_shift4,
                        SUM(vs.unit_cost * pc.quantity) AS vendor_cost
                    FROM shift4.order_items oi
                    LEFT JOIN lpg.product_components pc
                        ON pc.product_sku = oi.sku
                    LEFT JOIN lpg.vendor_skus vs
                        ON vs.vendor_sku_id = pc.vendor_sku_id
                    WHERE oi.shift4_order_id = %s
                    GROUP BY oi.id, oi.sku, oi.quantity, oi.unit_price,
                             oi.item_unit_cost_shift4
                    ORDER BY oi.id
                    """,
                    (order_id,),
                )
                order["items"] = [
                    {
                        "sku": r[1],
                        "quantity": r[2],
                        "unit_price": str(r[3]),
                        "unit_cost_shift4": str(r[4]) if r[4] is not None else None,
                        "vendor_cost": str(r[5]) if r[5] is not None else None,
                    }
                    for r in cur.fetchall()
                ]

                cur.execute(
                    """
                    SELECT shift4_shipment_id,
                        ship_first_name, ship_last_name, ship_company,
                        ship_address, ship_address2, ship_city, ship_state,
                        ship_zip, ship_country, ship_phone, ship_email,
                        shipment_method_name, customer_shipping_cost, tracking_code
                    FROM shift4.shipments
                    WHERE shift4_order_id = %s
                    ORDER BY shift4_shipment_id
                    """,
                    (order_id,),
                )
                order["shipments"] = [
                    {
                        "shipment_id": r[0],
                        "first_name": r[1],
                        "last_name": r[2],
                        "company": r[3],
                        "address": r[4],
                        "address2": r[5],
                        "city": r[6],
                        "state": r[7],
                        "zip": r[8],
                        "country": r[9],
                        "phone": r[10],
                        "email": r[11],
                        "method": r[12],
                        "shipping_cost": str(r[13]) if r[13] is not None else None,
                        "tracking_code": r[14],
                    }
                    for r in cur.fetchall()
                ]
            finally:
                cur.close()
    except Exception as exc:
        log.error("order_detail_db_error", error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"error": "database error"},
        )

    return order

async def get_order_html(order_id: int, request: Request):
    """HTML view of a single order. Same auth/data as /orders/{id}."""
    response_data = await get_order(order_id, request)
    if isinstance(response_data, JSONResponse):
        return response_data

    o = response_data

    items_html = "".join(
        f"""
        <tr>
            <td>{i['sku']}</td>
            <td style="text-align:right">{i['quantity']}</td>
            <td style="text-align:right">${i['unit_price']}</td>
            <td style="text-align:right">{'<strong>$' + i['vendor_cost'] + '</strong>' if i['vendor_cost'] else '<span style="color:#bbb">— not mapped</span>'}</td>
        </tr>
        """
        for i in o["items"]
    )

    shipments_html = "".join(
        f"""
        <div class="card">
            <strong>Shipment {s['shipment_id']}</strong> &mdash; {s['method'] or '(no method)'}<br>
            {s['first_name']} {s['last_name']}<br>
            {s['company'] or ''}<br>
            {s['address']}{', ' + s['address2'] if s['address2'] else ''}<br>
            {s['city']}, {s['state']} {s['zip']} {s['country']}<br>
            <span class="meta">Cost: ${s['shipping_cost'] or '0.00'} &middot; Tracking: {s['tracking_code'] or '(none)'}</span>
        </div>
        """
        for s in o["shipments"]
    )

    b = o["billing"]
    t = o["totals"]

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Order {o['shift4_order_id']} — LPG</title>
    <style>
        body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 900px; margin: 2em auto; padding: 0 1em; }}
        h1 {{ font-weight: 500; margin-bottom: 0; }}
        h2 {{ font-weight: 500; font-size: 1.1em; margin-top: 2em; border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ padding: 8px 12px; border-bottom: 1px solid #eee; text-align: left; }}
        th {{ background: #f5f5f5; font-weight: 500; }}
        .meta {{ color: #888; font-size: 0.9em; }}
        .card {{ border: 1px solid #e0e0e0; border-radius: 4px; padding: 12px 16px; margin: 8px 0; }}
        .totals td:first-child {{ width: 70%; text-align: right; }}
        .totals td:last-child {{ text-align: right; font-variant-numeric: tabular-nums; }}
        .totals tr.grand td {{ font-weight: 600; border-top: 2px solid #333; }}
        .back {{ color: #888; }}
    </style>
</head>
<body>
    <p class="back"><a href="javascript:history.back()">&larr; Back</a></p>
    <h1>Order {o['shift4_order_id']}</h1>
    <p class="meta">{o['invoice_number'] or ''} &middot; {o['order_status']} &middot; {o['order_date'] or ''}</p>

    <h2>Customer</h2>
    <p>
        {b['first_name']} {b['last_name']}<br>
        {b['company'] or ''}<br>
        {b['address']}{', ' + b['address2'] if b['address2'] else ''}<br>
        {b['city']}, {b['state']} {b['zip']} {b['country']}<br>
        <span class="meta">{b['email'] or '(no email)'} &middot; {b['phone'] or '(no phone)'}</span>
    </p>

    <h2>Items</h2>
    <table>
        <thead>
            <tr>
                <th>SKU</th>
                <th style="text-align:right">Qty</th>
                <th style="text-align:right">Unit Price</th>
                <th style="text-align:right">Real Cost</th>
            </tr>
        </thead>
        </thead>
        <tbody>{items_html}</tbody>
    </table>

    <h2>Shipments</h2>
    {shipments_html or '<p class="meta">(no shipments)</p>'}

    <h2>Totals</h2>
    <table class="totals">
        <tr><td>Subtotal</td><td>${t['subtotal']}</td></tr>
        <tr><td>Tax</td><td>${t['tax']}</td></tr>
        <tr><td>Shipping</td><td>${t['shipping_cost']}</td></tr>
        <tr><td>Discount</td><td>${t['discount']}</td></tr>
        <tr class="grand"><td>Grand Total</td><td>${t['grand_total']}</td></tr>
    </table>

    {f'<h2>Comments</h2><p>{o["comments"]}</p>' if o['comments'] else ''}
</body>
</html>"""

    return HTMLResponse(content=html)

# Read endpoints are registered only when running as the lpg-admin
# service (IAM-protected) or locally for development (no K_SERVICE).
# On webhook-handler in production, these routes simply don't exist —
# Cloud Run returns FastAPI's default 404 with no application code
# running. See ADR-0015.
import os
_K_SERVICE = os.getenv("K_SERVICE")
if is_admin_service() or _K_SERVICE is None:
    app.add_api_route("/orders", list_orders, methods=["GET"])
    app.add_api_route(
        "/orders.html", list_orders_html, methods=["GET"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/orders/{order_id:int}", get_order, methods=["GET"],
    )
    
    app.add_api_route(
        "/orders/{order_id}.html", get_order_html, methods=["GET"],
        response_class=HTMLResponse,
    )


@app.get("/webhooks/shift4/order-created")
async def shift4_order_created_probe():
    """Respond 200 to Shift4's pre-POST GET probe."""
    return {"status": "ready", "method": "GET", "expects": "POST"}


@app.post("/orders/{shift4_order_id}/purchase-order")
async def generate_order_purchase_order(shift4_order_id: int, request: Request):
    """Generate (or regenerate) a draft Crown PO for an order. **lpg-admin only.**

    This is a mutating, admin-only operation. It is IAM-protected by Cloud Run on
    the lpg-admin service; on the public webhook-handler service it returns 404 so
    PO generation is never exposed publicly.

    Optional JSON body carries manual fees (ADR-0018 Q2 — fees are manual):
        {"order_fee": "15.00", "broken_carton_fee": "15.00"}

    Returns the draft PO as JSON. Regeneration updates the PO in place and resets
    its status to draft. Responds 404 if the order doesn't exist, 422 on a bad
    body, 500 on a database error.
    """
    if not is_admin_service():
        # Not reachable on the public service.
        return JSONResponse(status_code=404, content={"error": "not found"})

    # Parse optional manual fees from the body.
    fees: list[Fee] = []
    raw = await request.body()
    if raw:
        try:
            body = json.loads(raw)
            for key, label in (("order_fee", "Order Fee"),
                               ("broken_carton_fee", "Broken Carton Fee")):
                value = body.get(key)
                if value is not None:
                    fees.append(Fee(label, Decimal(str(value))))
        except (json.JSONDecodeError, ArithmeticError, ValueError, AttributeError) as exc:
            return JSONResponse(
                status_code=422,
                content={"error": f"invalid request body: {exc}"},
            )

    structlog.contextvars.bind_contextvars(shift4_order_id=shift4_order_id)
    try:
        with get_connection() as conn:
            po, result = generate_purchase_order(conn, shift4_order_id, fees=fees)
            # get_connection commits on clean exit.
    except PurchaseOrderError as exc:
        # Order/vendor not found, etc. — caller error, and the transaction was
        # rolled back by get_connection when the exception propagated.
        log.warning("po_generate_not_possible", error=str(exc))
        return JSONResponse(status_code=404, content={"error": str(exc)})
    except Exception as exc:
        log.error("po_generate_db_error", error=str(exc), exc_info=True)
        return JSONResponse(status_code=500, content={"error": "database error"})

    log.info(
        "po_generated",
        po_number=po.po_number,
        regenerated=result.regenerated,
        lines=result.line_count,
        unpriced=len(result.unpriced_skus),
    )
    return purchase_order_to_dict(po, result)
