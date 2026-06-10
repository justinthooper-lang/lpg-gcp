#!/usr/bin/env bash
#
# Build and deploy the webhook-handler image to both Cloud Run services.
#
# Usage:
#   ./scripts/deploy.sh v0.12.0
#
# Both services share the same image. Deploying to only one would leave
# them on different revisions, which the K_SERVICE-based auth and
# route-registration logic depends on being consistent. Use this script
# to keep them in sync.
#
# After deploy, the script runs a smoke-test matrix against the
# production endpoints. If any check fails, the script exits non-zero
# and prints the offending result.

set -euo pipefail

REGION="us-west1"
PROJECT="lpg-dev-496820"
IMAGE_REPO="us-west1-docker.pkg.dev/${PROJECT}/lpg-images/webhook-handler"
WEBHOOK_URL="https://webhook-handler-388123220900.us-west1.run.app"
ADMIN_URL="https://lpg-admin-388123220900.us-west1.run.app"

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <version-tag>" >&2
    echo "Example: $0 v0.12.0" >&2
    exit 1
fi

VERSION="$1"

if [[ ! "${VERSION}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: version must look like vX.Y.Z (got: ${VERSION})" >&2
    exit 1
fi

FULL_IMAGE="${IMAGE_REPO}:${VERSION}"

echo "=== Build ==="
echo "Image: ${FULL_IMAGE}"
cd "$(dirname "$0")/.."
gcloud builds submit \
    --config cloudbuild.webhook.yaml \
    --substitutions=_TAG="${VERSION}" \
    .

echo
echo "=== Deploy webhook-handler ==="
gcloud run deploy webhook-handler \
    --image="${FULL_IMAGE}" \
    --region="${REGION}"

echo
echo "=== Deploy lpg-admin ==="
gcloud run deploy lpg-admin \
    --image="${FULL_IMAGE}" \
    --region="${REGION}"

echo
echo "=== Smoke tests ==="

# Helper: assert curl status code matches expected.
check() {
    local label="$1"
    local expected="$2"
    local got="$3"
    if [[ "${got}" == "${expected}" ]]; then
        echo "  ✓ ${label}: ${got}"
    else
        echo "  ✗ ${label}: expected ${expected}, got ${got}" >&2
        exit 1
    fi
}

URL_TOKEN="$(gcloud secrets versions access latest --secret=shift4-webhook-token)"
ID_TOKEN="$(gcloud auth print-identity-token)"

# 1. webhook-handler GET /orders should be 404 (route doesn't exist there)
code="$(curl -s -o /dev/null -w '%{http_code}' "${WEBHOOK_URL}/orders?token=${URL_TOKEN}")"
check "webhook-handler /orders (should be 404)" "404" "${code}"

# 2. webhook-handler webhook probe (GET) should be 200
code="$(curl -s -o /dev/null -w '%{http_code}' "${WEBHOOK_URL}/webhooks/shift4/order-created")"
check "webhook-handler GET probe" "200" "${code}"

# 3. lpg-admin /orders without auth should be 403 (Google frontend)
code="$(curl -s -o /dev/null -w '%{http_code}' "${ADMIN_URL}/orders")"
check "lpg-admin no-auth (should be 403)" "403" "${code}"

# 4. lpg-admin /orders with IAM should be 200
code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${ID_TOKEN}" "${ADMIN_URL}/orders")"
check "lpg-admin IAM auth" "200" "${code}"

unset URL_TOKEN ID_TOKEN

echo
echo "=== Done ==="
echo "Both services deployed at ${VERSION}, all smoke tests passed."