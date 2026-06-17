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
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pg8000.exceptions import DatabaseError, InterfaceError
from pydantic import ValidationError
from auth import verify_token, is_authorized_read, is_admin_service
from lpg_common.db import get_connection
from ingest import ingest_order
from logging_config import configure_logging
from shift4_models import ORDER_STATUS_MAP, Shift4OrderPayload
from decimal import Decimal
from purchase_order_builder import Fee
from purchase_order_pdf import render_purchase_order_pdf
from graph_mail import GraphSendError, send_purchase_order_email
from gcs_storage import GcsStorageError, upload_po_pdf
from po_composer import PO_COMPOSER_TEMPLATE
from order_overrides_form import ORDER_OVERRIDES_TEMPLATE
from order_economics_form import ORDER_ECONOMICS_TEMPLATE
from purchase_order_repository import (
    PurchaseOrderError,
    PurchaseOrderImmutable,
    POLineError,
    add_po_line,
    delete_po_line,
    generate_purchase_order,
    get_purchase_order_status,
    get_vendor_po_email,
    load_purchase_order,
    mark_purchase_order_sent,
    po_editable_state,
    purchase_order_to_dict,
    update_po_line,
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
                    FROM lpg.v_orders_effective
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
                        updated_at, has_override
                    FROM lpg.v_orders_effective
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
                    "has_override": row[23],
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

    composer_html = PO_COMPOSER_TEMPLATE.replace("__ORDER_ID__", str(order_id))
    overrides_html = ORDER_OVERRIDES_TEMPLATE.replace("__ORDER_ID__", str(order_id))
    economics_html = ORDER_ECONOMICS_TEMPLATE.replace("__ORDER_ID__", str(order_id))

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{o['invoice_number'] or ('Order ' + str(o['shift4_order_id']))} — LPG</title>
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
    <h1>{o['invoice_number'] or ('Order ' + str(o['shift4_order_id']))}</h1>
    <p class="meta">{o['order_status']} &middot; {o['order_date'] or ''} &middot; <span title="Shift4 internal order id">#{o['shift4_order_id']}</span></p>

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

    {overrides_html}

    {economics_html}

    {composer_html}
</body>
</html>"""

    return HTMLResponse(content=html)


# --- Order overrides (ADR-0021): LPG-owned corrections over the mirror ---
# Columns that lpg.v_orders_effective overlays; must match the override table
# and the view. The metadata columns (override_reason, overridden_by) are
# handled separately and are not part of the COALESCE overlay.
_OVERRIDE_OVERLAY_COLUMNS = (
    "bill_first_name", "bill_last_name", "bill_company", "bill_address",
    "bill_address2", "bill_city", "bill_state", "bill_zip", "bill_country",
    "bill_phone", "bill_email",
    "ship_to_first_name", "ship_to_last_name", "ship_to_company",
    "ship_to_address", "ship_to_address2", "ship_to_city", "ship_to_state",
    "ship_to_zip", "ship_to_country", "ship_to_phone",
    "comments",
)


def _overrides_payload(cur, order_id: int) -> dict | None:
    """Build the {mirror, override, has_override} payload for an order.

    Returns None if the order does not exist in the shift4.orders mirror.
    """
    cols = ", ".join(_OVERRIDE_OVERLAY_COLUMNS)
    cur.execute(
        f"SELECT {cols} FROM shift4.orders WHERE shift4_order_id = %s",
        (order_id,),
    )
    m = cur.fetchone()
    if m is None:
        return None
    mirror = dict(zip(_OVERRIDE_OVERLAY_COLUMNS, m))

    cur.execute(
        f"SELECT {cols}, override_reason FROM lpg.order_overrides "
        f"WHERE shift4_order_id = %s",
        (order_id,),
    )
    o = cur.fetchone()
    if o is None:
        override = {c: None for c in _OVERRIDE_OVERLAY_COLUMNS}
        override["override_reason"] = None
        has_override = False
    else:
        override = dict(zip(_OVERRIDE_OVERLAY_COLUMNS, o[:-1]))
        override["override_reason"] = o[-1]
        has_override = True

    return {
        "shift4_order_id": order_id,
        "mirror": mirror,
        "override": override,
        "has_override": has_override,
    }


async def get_order_overrides(order_id: int, request: Request):
    """Mirror + current override values for an order (read).

    Same auth as the other read endpoints (IAM on lpg-admin, token elsewhere).
    """
    if not is_authorized_read(request.query_params.get("token")):
        return JSONResponse(
            status_code=401, content={"error": "invalid or missing token"}
        )
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            try:
                payload = _overrides_payload(cur, order_id)
            finally:
                cur.close()
    except Exception as exc:
        log.error("order_overrides_read_error", error=str(exc))
        return JSONResponse(status_code=500, content={"error": "database error"})
    if payload is None:
        return JSONResponse(
            status_code=404, content={"error": f"order {order_id} not found"}
        )
    return payload


async def save_order_overrides(order_id: int, request: Request):
    """Upsert (or delete) an order's override row. **lpg-admin only.**

    Writes ONLY to lpg.order_overrides — never the shift4.orders mirror
    (ADR-0021). Blank fields are stored as NULL (inherit from the mirror). If
    every overlay field is blank, any existing override row is deleted (full
    revert to storefront). IAM-protected on lpg-admin; 404 on the public service.
    """
    if not is_admin_service():
        return JSONResponse(status_code=404, content={"error": "not found"})

    try:
        body = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError as exc:
        return JSONResponse(
            status_code=422, content={"error": f"invalid request body: {exc}"}
        )

    def _clean(value):
        if value is None:
            return None
        s = str(value).strip()
        return s or None

    values = {c: _clean(body.get(c)) for c in _OVERRIDE_OVERLAY_COLUMNS}
    reason = _clean(body.get("override_reason"))
    all_blank = all(v is None for v in values.values())
    actor = request.headers.get("X-Goog-Authenticated-User-Email") or "lpg-admin"

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT 1 FROM shift4.orders WHERE shift4_order_id = %s",
                    (order_id,),
                )
                if cur.fetchone() is None:
                    return JSONResponse(
                        status_code=404,
                        content={"error": f"order {order_id} not found"},
                    )

                if all_blank:
                    # Nothing to overlay — revert by deleting any existing row.
                    # A reason with no field corrections is not a real override.
                    cur.execute(
                        "DELETE FROM lpg.order_overrides WHERE shift4_order_id = %s",
                        (order_id,),
                    )
                else:
                    cols = list(_OVERRIDE_OVERLAY_COLUMNS)
                    meta = ["override_reason", "overridden_by"]
                    insert_cols = ["shift4_order_id"] + cols + meta
                    placeholders = ", ".join(["%s"] * len(insert_cols))
                    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols + meta)
                    cur.execute(
                        f"""
                        INSERT INTO lpg.order_overrides ({", ".join(insert_cols)})
                        VALUES ({placeholders})
                        ON CONFLICT (shift4_order_id) DO UPDATE SET
                            {updates}, updated_at = NOW()
                        """,
                        [order_id] + [values[c] for c in cols] + [reason, actor],
                    )

                payload = _overrides_payload(cur, order_id)
            finally:
                cur.close()
            # get_connection commits on clean exit.
    except Exception as exc:
        log.error("order_overrides_write_error", error=str(exc), exc_info=True)
        return JSONResponse(status_code=500, content={"error": "database error"})

    log.info(
        "order_overrides_saved",
        shift4_order_id=order_id,
        has_override=payload["has_override"],
        actor=actor,
    )
    return payload


def _margin_payload(cur, order_id: int):
    """Effective margin (from v_order_margins) + any manual entry for an order.

    Returns None if the order doesn't exist. margin_source is 'invoice'
    (locked — real Crown invoice), 'manual', or 'none' (editable). The manual_*
    fields echo what's stored in lpg.order_margin_manual (may differ from the
    effective values when an invoice supersedes them).
    """
    cur.execute(
        """
        SELECT m.margin_source, m.has_invoice, m.has_manual,
               m.grand_total, m.shipping_cost,
               m.supplier_cost, m.actual_freight,
               m.profit, m.shipping_differential,
               mm.manual_supplier_cost, mm.manual_freight, mm.note
        FROM lpg.v_order_margins m
        LEFT JOIN lpg.order_margin_manual mm ON mm.shift4_order_id = m.shift4_order_id
        WHERE m.shift4_order_id = %s
        """,
        (order_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None

    def _f(v):
        return None if v is None else float(v)

    return {
        "shift4_order_id": order_id,
        "margin_source": row[0],
        "has_invoice": row[1],
        "has_manual": row[2],
        "editable": (row[0] != "invoice"),   # invoice always wins; manual locked out
        "grand_total": _f(row[3]),
        "shipping_cost": _f(row[4]),
        "supplier_cost": _f(row[5]),
        "actual_freight": _f(row[6]),
        "profit": _f(row[7]),
        "shipping_differential": _f(row[8]),
        "manual_supplier_cost": _f(row[9]),
        "manual_freight": _f(row[10]),
        "note": row[11],
    }


async def get_order_margin(order_id: int, request: Request):
    """Effective margin + manual entry for an order (read). Same auth as reads."""
    if not is_authorized_read(request.query_params.get("token")):
        return JSONResponse(
            status_code=401, content={"error": "invalid or missing token"}
        )
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            try:
                payload = _margin_payload(cur, order_id)
            finally:
                cur.close()
    except Exception as exc:
        log.error("order_margin_read_error", error=str(exc))
        return JSONResponse(status_code=500, content={"error": "database error"})
    if payload is None:
        return JSONResponse(
            status_code=404, content={"error": f"order {order_id} not found"}
        )
    return payload


async def save_order_margin(order_id: int, request: Request):
    """Upsert (or clear) an order's MANUAL margin entry. **lpg-admin only.**

    Gap-filler only (ADR-0025): refuses to write when the order already has a
    matched Crown invoice (margin_source='invoice') — invoice-true data is never
    overridden. Writes both manual_supplier_cost and manual_freight together (a
    margin needs both); blank/omitted = clear the manual row entirely.
    """
    if not is_admin_service():
        return JSONResponse(status_code=404, content={"error": "not found"})

    try:
        body = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError as exc:
        return JSONResponse(
            status_code=422, content={"error": f"invalid request body: {exc}"}
        )

    def _num(v):
        if v is None or str(v).strip() == "":
            return None
        try:
            n = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"not a number: {v!r}")
        if n < 0:
            raise ValueError("must be >= 0")
        return n

    try:
        cost = _num(body.get("manual_supplier_cost"))
        freight = _num(body.get("manual_freight"))
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})

    note = (str(body.get("note")).strip() or None) if body.get("note") else None
    clear = cost is None and freight is None
    # A partial entry (only one of the two) can't form a margin — reject it.
    if not clear and (cost is None or freight is None):
        return JSONResponse(
            status_code=422,
            content={"error": "both manual_supplier_cost and manual_freight are required"},
        )

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT 1 FROM shift4.orders WHERE shift4_order_id = %s",
                    (order_id,),
                )
                if cur.fetchone() is None:
                    return JSONResponse(
                        status_code=404,
                        content={"error": f"order {order_id} not found"},
                    )

                # Refuse to write a manual entry over a real invoice.
                cur.execute(
                    "SELECT margin_source FROM lpg.v_order_margins WHERE shift4_order_id = %s",
                    (order_id,),
                )
                src_row = cur.fetchone()
                if src_row and src_row[0] == "invoice" and not clear:
                    return JSONResponse(
                        status_code=409,
                        content={"error": "order has a matched Crown invoice; manual entry not allowed"},
                    )

                if clear:
                    cur.execute(
                        "DELETE FROM lpg.order_margin_manual WHERE shift4_order_id = %s",
                        (order_id,),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO lpg.order_margin_manual
                            (shift4_order_id, manual_supplier_cost, manual_freight, note)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (shift4_order_id) DO UPDATE SET
                            manual_supplier_cost = EXCLUDED.manual_supplier_cost,
                            manual_freight = EXCLUDED.manual_freight,
                            note = EXCLUDED.note,
                            updated_at = NOW()
                        """,
                        (order_id, cost, freight, note),
                    )

                payload = _margin_payload(cur, order_id)
            finally:
                cur.close()
    except Exception as exc:
        log.error("order_margin_write_error", error=str(exc), exc_info=True)
        return JSONResponse(status_code=500, content={"error": "database error"})

    actor = request.headers.get("X-Goog-Authenticated-User-Email") or "lpg-admin"
    log.info(
        "order_margin_saved",
        shift4_order_id=order_id,
        cleared=clear,
        margin_source=payload["margin_source"],
        actor=actor,
    )
    return payload


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
    app.add_api_route(
        "/orders/{order_id:int}/overrides", get_order_overrides, methods=["GET"],
    )
    app.add_api_route(
        "/orders/{order_id:int}/overrides", save_order_overrides, methods=["POST"],
    )
    app.add_api_route(
        "/orders/{order_id:int}/margin", get_order_margin, methods=["GET"],
    )
    app.add_api_route(
        "/orders/{order_id:int}/margin", save_order_margin, methods=["POST"],
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

    Regeneration is destructive — it rebuilds lines from the order. To protect
    work, it refuses (409) to overwrite a PO that has been hand-edited
    (manually_edited) or already sent, unless ``?force=true`` (ADR-0022). A forced
    regeneration resets manually_edited to false.
    """
    if not is_admin_service():
        # Not reachable on the public service.
        return JSONResponse(status_code=404, content={"error": "not found"})

    force = request.query_params.get("force", "").lower() in ("1", "true", "yes")

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
            # Guard: don't silently clobber a hand-edited or sent PO (ADR-0022).
            if not force:
                cur = conn.cursor()
                cur.execute(
                    "SELECT po_number, manually_edited, status FROM lpg.purchase_orders "
                    "WHERE shift4_order_id = %s",
                    (shift4_order_id,),
                )
                existing = cur.fetchone()
                if existing is not None:
                    po_num, edited, status = existing
                    if status == "sent":
                        return JSONResponse(status_code=409, content={
                            "error": f"PO {po_num} has been sent; pass ?force=true to "
                                     "regenerate (this discards the sent draft)"})
                    if edited:
                        return JSONResponse(status_code=409, content={
                            "error": f"PO {po_num} has manual edits; pass ?force=true to "
                                     "regenerate from the order (this discards your edits)"})
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


# --- Editable draft-PO lines (ADR-0022). All lpg-admin only. --------------- #
def _run_po_line_op(op):
    """Run a repository line op in a transaction, mapping errors to HTTP.

    ``op`` is a callable taking the open connection and returning the editable
    state dict (or None when the PO doesn't exist).
    """
    try:
        with get_connection() as conn:
            state = op(conn)
            # get_connection commits on clean exit.
        if state is None:
            return JSONResponse(status_code=404, content={"error": "purchase order not found"})
        return state
    except POLineError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})
    except PurchaseOrderImmutable as exc:
        return JSONResponse(status_code=409, content={"error": str(exc)})
    except PurchaseOrderError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})
    except Exception as exc:
        log.error("po_line_op_error", error=str(exc), exc_info=True)
        return JSONResponse(status_code=500, content={"error": "database error"})


async def _po_line_body(request: Request):
    """Parse a line JSON body, or return (None, error-response)."""
    try:
        return json.loads(await request.body() or b"{}"), None
    except json.JSONDecodeError as exc:
        return None, JSONResponse(
            status_code=422, content={"error": f"invalid request body: {exc}"}
        )


@app.get("/purchase-orders/{po_number}/lines")
async def list_purchase_order_lines(po_number: str):
    """Editable state of a PO (status, manually_edited, lines with ids, total).
    **lpg-admin only.**"""
    if not is_admin_service():
        return JSONResponse(status_code=404, content={"error": "not found"})
    return _run_po_line_op(lambda conn: po_editable_state(conn, po_number))


@app.post("/purchase-orders/{po_number}/lines")
async def add_purchase_order_line(po_number: str, request: Request):
    """Append a line to a draft PO. **lpg-admin only.** 409 if sent, 422 on bad shape."""
    if not is_admin_service():
        return JSONResponse(status_code=404, content={"error": "not found"})
    body, err = await _po_line_body(request)
    if err:
        return err
    return _run_po_line_op(lambda conn: add_po_line(
        conn, po_number,
        is_fee=bool(body.get("is_fee", False)),
        vendor_sku_code=body.get("vendor_sku_code"),
        description=body.get("description"),
        quantity=body.get("quantity"),
        unit_cost=body.get("unit_cost"),
        amount=body.get("amount"),
    ))


@app.patch("/purchase-orders/{po_number}/lines/{line_id}")
async def edit_purchase_order_line(po_number: str, line_id: int, request: Request):
    """Replace a draft PO line's fields. **lpg-admin only.** 409 if sent, 422 on bad shape."""
    if not is_admin_service():
        return JSONResponse(status_code=404, content={"error": "not found"})
    body, err = await _po_line_body(request)
    if err:
        return err
    return _run_po_line_op(lambda conn: update_po_line(
        conn, po_number, line_id,
        is_fee=body.get("is_fee"),
        vendor_sku_code=body.get("vendor_sku_code"),
        description=body.get("description"),
        quantity=body.get("quantity"),
        unit_cost=body.get("unit_cost"),
        amount=body.get("amount"),
    ))


@app.delete("/purchase-orders/{po_number}/lines/{line_id}")
async def remove_purchase_order_line(po_number: str, line_id: int):
    """Delete a draft PO line. **lpg-admin only.** 409 if sent."""
    if not is_admin_service():
        return JSONResponse(status_code=404, content={"error": "not found"})
    return _run_po_line_op(lambda conn: delete_po_line(conn, po_number, line_id))


@app.get("/purchase-orders/{po_number}/pdf")
async def get_purchase_order_pdf(po_number: str):
    """Render a stored draft PO to a PDF. **lpg-admin only.**

    IAM-protected by Cloud Run on lpg-admin; returns 404 on the public
    webhook-handler service. Loads the persisted PO (header + lines) and renders
    it with the reportlab builder. Responds 404 if the PO doesn't exist, 500 on
    a database error.
    """
    if not is_admin_service():
        return JSONResponse(status_code=404, content={"error": "not found"})

    try:
        with get_connection() as conn:
            po = load_purchase_order(conn, po_number)
    except PurchaseOrderError as exc:
        log.warning("po_pdf_not_found", po_number=po_number, error=str(exc))
        return JSONResponse(status_code=404, content={"error": str(exc)})
    except Exception as exc:
        log.error("po_pdf_db_error", error=str(exc), exc_info=True)
        return JSONResponse(status_code=500, content={"error": "database error"})

    pdf_bytes = render_purchase_order_pdf(po)
    log.info("po_pdf_rendered", po_number=po_number, bytes=len(pdf_bytes))
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{po_number}.pdf"'},
    )


@app.post("/purchase-orders/{po_number}/send")
async def send_purchase_order(po_number: str, request: Request):
    """Email a stored PO's PDF to the vendor and mark it sent. **lpg-admin only.**

    IAM-protected on lpg-admin; 404 on the public service. Guards against accidental
    double-send: if the PO is already 'sent', returns 409 unless ?force=true.

    Responds 404 if the PO doesn't exist, 422 if the vendor has no po_email, 502 if
    the Graph send fails, 500 on a database error.
    """
    if not is_admin_service():
        return JSONResponse(status_code=404, content={"error": "not found"})

    force = request.query_params.get("force", "").lower() in ("1", "true", "yes")

    try:
        with get_connection() as conn:
            po = load_purchase_order(conn, po_number)  # PurchaseOrderError -> 404

            if get_purchase_order_status(conn, po_number) == "sent" and not force:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": f"PO {po_number} already sent; pass ?force=true to resend"
                    },
                )

            recipient = get_vendor_po_email(conn, po.vendor_id)
            if not recipient:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": f"vendor {po.vendor_id} has no po_email; cannot send"
                    },
                )

            pdf_bytes = render_purchase_order_pdf(po)

            try:
                mailbox = send_purchase_order_email(
                    recipient=recipient,
                    po_number=po_number,
                    pdf_bytes=pdf_bytes,
                )
            except GraphSendError as exc:
                # Nothing persisted yet; surface the send failure to the caller.
                log.error("po_send_graph_error", po_number=po_number, error=str(exc))
                return JSONResponse(
                    status_code=502,
                    content={"error": f"send failed: {exc}"},
                )

            # Archive the exact bytes that were emailed (best-effort): a storage
            # failure must never unwind a real send, so we log and proceed. The
            # email already went out — that is the source of truth for "sent".
            pdf_uri = None
            try:
                pdf_uri = upload_po_pdf(po_number, pdf_bytes)
            except GcsStorageError as exc:
                log.warning("po_archive_failed", po_number=po_number, error=str(exc))

            mark_purchase_order_sent(conn, po_number, pdf_uri=pdf_uri)
            # get_connection commits on clean exit.
    except PurchaseOrderError as exc:
        log.warning("po_send_not_found", po_number=po_number, error=str(exc))
        return JSONResponse(status_code=404, content={"error": str(exc)})
    except Exception as exc:
        log.error("po_send_db_error", error=str(exc), exc_info=True)
        return JSONResponse(status_code=500, content={"error": "database error"})

    log.info(
        "po_sent",
        po_number=po_number,
        recipient=recipient,
        mailbox=mailbox,
        archived=bool(pdf_uri),
    )
    return {
        "sent": True,
        "po_number": po_number,
        "recipient": recipient,
        "mailbox": mailbox,
        "archived_pdf": pdf_uri,
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_html(request: Request):
    """Margin dashboard: MTD/YTD revenue + profit and undercharged-shipping.

    Reads lpg.v_order_margins (ADR-0024). Same token auth as the other read
    routes. Margins are invoice-true; orders without a matched invoice are
    reported as 'cost pending' and excluded from profit/shipping aggregates.
    """
    received_token = request.query_params.get("token")
    if not is_authorized_read(received_token):
        return JSONResponse(status_code=401, content={"error": "invalid or missing token"})

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            try:
                # Period rollups: current month (MTD) and current year (YTD).
                # Revenue counts all orders; profit/shipping only invoice-matched.
                cur.execute(
                    """
                    WITH m AS (SELECT * FROM lpg.v_order_margins)
                    SELECT
                        period,
                        count(*)                                   AS orders,
                        count(*) FILTER (WHERE has_invoice)        AS matched,
                        count(*) FILTER (WHERE NOT has_invoice)    AS pending,
                        coalesce(sum(grand_total), 0)              AS revenue,
                        coalesce(sum(grand_total) FILTER (WHERE has_invoice), 0) AS matched_revenue,
                        coalesce(sum(profit) FILTER (WHERE has_invoice), 0)   AS profit,
                        count(*) FILTER (WHERE shipping_differential < 0)     AS undercharged
                    FROM (
                        SELECT *, 'YTD' AS period FROM m
                          WHERE order_date >= date_trunc('year', now())
                        UNION ALL
                        SELECT *, 'MTD' AS period FROM m
                          WHERE order_date >= date_trunc('month', now())
                    ) x
                    GROUP BY period
                    """
                )
                periods = {r[0]: r for r in cur.fetchall()}

                # Undercharged-shipping detail: orders where we ate freight.
                cur.execute(
                    """
                    SELECT shift4_order_id, invoice_number, order_date::date, grand_total,
                           shipping_cost, actual_freight, shipping_differential, profit
                    FROM lpg.v_order_margins
                    WHERE shipping_differential < 0
                      AND order_date >= date_trunc('year', now())
                    ORDER BY shipping_differential ASC
                    LIMIT 200
                    """
                )
                undercharged = cur.fetchall()
            finally:
                cur.close()
    except Exception as exc:
        log.error("dashboard_db_error", error=str(exc))
        return JSONResponse(status_code=500, content={"error": "database error"})

    def card(period_key, label):
        r = periods.get(period_key)
        if not r:
            return f'<div class="card"><strong>{label}</strong><br><span class="meta">no data</span></div>'
        _, orders, matched, pending, revenue, matched_revenue, profit, undercharged_n = r
        # Margin % is over MATCHED orders only (revenue and profit both restricted
        # to orders we actually have a supplier invoice for) so it isn't diluted by
        # pending orders that have revenue but no cost yet.
        margin_pct = (float(profit) / float(matched_revenue) * 100) if matched_revenue else 0
        return f"""
        <div class="card">
            <strong>{label}</strong>
            <table class="kpi">
                <tr><td>Revenue</td><td>${revenue:,.2f}</td></tr>
                <tr><td>Profit <span class="meta">(matched)</span></td><td>${profit:,.2f}</td></tr>
                <tr><td>Margin <span class="meta">(of matched rev)</span></td><td>{margin_pct:.1f}%</td></tr>
                <tr><td>Matched revenue</td><td>${matched_revenue:,.2f}</td></tr>
                <tr><td>Orders</td><td>{orders} <span class="meta">({matched} matched, {pending} pending)</span></td></tr>
                <tr><td>Undercharged shipping</td><td>{undercharged_n}</td></tr>
            </table>
        </div>
        """

    rows_html = "".join(
        f"""
        <tr>
            <td><a href="/orders/{oid}.html">{inv or ''}</a></td>
            <td>{od}</td>
            <td style="text-align:right">${gt:,.2f}</td>
            <td style="text-align:right">${sc:,.2f}</td>
            <td style="text-align:right">${af:,.2f}</td>
            <td style="text-align:right; color:#c0392b">${sd:,.2f}</td>
            <td style="text-align:right">${pr:,.2f}</td>
        </tr>
        """
        for (oid, inv, od, gt, sc, af, sd, pr) in undercharged
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Margins — LPG</title>
    <style>
        body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 1000px; margin: 2em auto; padding: 0 1em; }}
        h1 {{ font-weight: 500; margin-bottom: 0; }}
        h2 {{ font-weight: 500; font-size: 1.1em; margin-top: 2em; border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
        .meta {{ color: #888; font-size: 0.9em; }}
        .cards {{ display: flex; gap: 16px; margin-top: 1em; }}
        .card {{ flex: 1; border: 1px solid #e0e0e0; border-radius: 6px; padding: 14px 18px; }}
        table.kpi {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
        table.kpi td {{ padding: 4px 0; }}
        table.kpi td:last-child {{ text-align: right; font-variant-numeric: tabular-nums; font-weight: 500; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ padding: 8px 12px; border-bottom: 1px solid #eee; text-align: left; }}
        th {{ background: #f5f5f5; font-weight: 500; }}
        td {{ font-variant-numeric: tabular-nums; }}
    </style>
</head>
<body>
    <h1>Margins</h1>
    <p class="meta">Invoice-true margins from lpg.v_order_margins &middot; profit = revenue &minus; supplier cost &minus; actual freight</p>

    <div class="cards">
        {card('MTD', 'This Month')}
        {card('YTD', 'Year to Date')}
    </div>

    <h2>Undercharged shipping &mdash; YTD ({len(undercharged)})</h2>
    <p class="meta">Orders where the freight we paid exceeded the shipping we charged (most negative first).</p>
    <table>
        <thead>
            <tr>
                <th>Order</th><th>Date</th>
                <th style="text-align:right">Grand Total</th>
                <th style="text-align:right">Ship Charged</th>
                <th style="text-align:right">Actual Freight</th>
                <th style="text-align:right">Ship Diff</th>
                <th style="text-align:right">Profit</th>
            </tr>
        </thead>
        <tbody>{rows_html or '<tr><td colspan="7" class="meta">none</td></tr>'}</tbody>
    </table>
</body>
</html>"""

    return HTMLResponse(content=html)
