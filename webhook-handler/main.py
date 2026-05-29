"""LPG webhook handler — Layer 2: webhook endpoint with validation.

Receives Shift4Shop Order New webhooks at /webhooks/shift4/order-created.
Validates the payload with Pydantic, then returns 200. Does not yet
write to the database — that's Layer 3.

Status filtering per ADR-0009: only OrderStatusID values in
ORDER_STATUS_MAP are ingested. Anything else (including OrderStatusID
21 / Quote) is acknowledged with 200 but skipped.
"""

from fastapi import FastAPI

from shift4_models import ORDER_STATUS_MAP, Shift4OrderPayload

from ingest import ingest_order

app = FastAPI(title="lpg-webhook-handler", version="0.2.0")


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
    422 automatically if validation fails. Returns 200 with a brief
    status summary on success.

    Currently: parses, classifies, returns. Does not write to DB.
    """
    status_id = payload.order_status_id
    status_text = ORDER_STATUS_MAP.get(status_id)

    if status_text is None:
        # Status not in our allow-list. Acknowledge but skip.
        return {
            "received": True,
            "ingested": False,
            "reason": f"order_status_id={status_id} not in allow-list",
            "order_id": payload.shift4_order_id,
        }

    if status_text == "Quote":
        # Quotes are filtered per ADR-0009 (they're not real orders).
        return {
            "received": True,
            "ingested": False,
            "reason": "quote status excluded by business rule",
            "order_id": payload.shift4_order_id,
        }

    # Status is one of New, Processing, Shipped. Persist to DB.
    try:
        result = ingest_order(payload)
    except Exception as exc:
        # Log and re-raise as a 500. FastAPI returns a generic error to
        # Shift4, which will retry per their webhook policy.
        # Layer 4 will replace this with structured logging.
        print(f"INGEST ERROR for order {payload.shift4_order_id}: {exc!r}")
        raise

    return {
        "received": True,
        "ingested": True,
        **result,
    }
    
