"""Smoke test for HMAC signature verification.

Run: python smoke_auth.py
"""

import hashlib
import hmac
import os

from auth import verify_signature, webhook_secret

# Force a known secret for this test
os.environ["SHIFT4_WEBHOOK_SECRET"] = "test-secret-shhh"

body = b'{"OrderID": 31990}'
secret = "test-secret-shhh"

# Compute the expected signature the same way Shift4 would
expected_sig = hmac.new(
    secret.encode("utf-8"),
    body,
    hashlib.sha256,
).hexdigest()

print(f"Body:           {body}")
print(f"Expected sig:   {expected_sig}")
print(f"Secret loaded:  {webhook_secret() == secret}")
print()

# Test 1: correct signature
print("Test 1: correct signature")
print(f"  → {verify_signature(body, expected_sig)}  (expect True)")
print()

# Test 2: wrong signature
print("Test 2: wrong signature")
print(f"  → {verify_signature(body, 'deadbeef')}  (expect False)")
print()

# Test 3: missing signature header
print("Test 3: missing signature header")
print(f"  → {verify_signature(body, None)}  (expect False)")
print()

# Test 4: tampered body
print("Test 4: tampered body (signature was for different body)")
tampered_body = b'{"OrderID": 99999}'
print(f"  → {verify_signature(tampered_body, expected_sig)}  (expect False)")
print()

# Test 5: case insensitivity
print("Test 5: uppercase signature should still match")
print(f"  → {verify_signature(body, expected_sig.upper())}  (expect True)")
