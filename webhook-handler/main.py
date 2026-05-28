"""LPG webhook handler — Layer 1: skeleton service.

This is the minimal FastAPI app to prove the toolchain works end-to-end.
No database, no validation, no real webhook handling — just enough to
hit a URL and get a response.
"""

from fastapi import FastAPI

app = FastAPI(title="lpg-webhook-handler", version="0.1.0")


@app.get("/")
def root():
    """Root endpoint. Useful for quick 'is the service alive' checks."""
    return {"service": "lpg-webhook-handler", "status": "ok"}


@app.get("/healthz")
def healthz():
    """Health check endpoint. Cloud Run uses this kind of route for
    liveness probes. The 'z' suffix is a Kubernetes convention — z
    because non-z names like /health were already commonly used by
    application code, so the platform took /healthz to avoid collision.
    """
    return {"status": "ok"}
    