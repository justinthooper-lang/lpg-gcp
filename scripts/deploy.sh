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
# Per ADR-0020, this script declares each service's FULL runtime shape on
# every deploy -- identity, scaling, Cloud SQL attachment, env vars, secret
# bindings, and the IAM invoker policy -- so the script (not the live
# resource) is the single source of truth for service shape, mirroring how
# deploy-job.sh manages the job. The shared shape lives in COMMON_FLAGS so the
# two services cannot drift apart; per-service env/secrets/invoker differ.
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

SERVICE_ACCOUNT="388123220900-compute@developer.gserviceaccount.com"
SQL_INSTANCE="${PROJECT}:${REGION}:lpg-dev"

# lpg-admin is IAM-private; this principal is the only invoker. Override per
# environment. webhook-handler is public (--allow-unauthenticated): the Shift4
# URL token authenticates requests, not Google IAM (ADR-0013).
ADMIN_INVOKER="user:justin.t.hooper@gmail.com"

# lpg-admin runtime config (PO send + GCS archive).
AZURE_TENANT_ID="fa215d01-a503-4496-ae9f-3ab71e89037e"
AZURE_SEND_CLIENT_ID="3e9eda8a-84ad-4bfe-bb94-9e3da4a1160d"
CROWN_PO_MAILBOX="customerservice@lamppostglobes.com"
PO_PDF_BUCKET="${PROJECT}-purchase-orders"

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

# Runtime shape shared by both services. Per-service env/secrets/invoker are
# set in the individual deploy commands below.
COMMON_FLAGS=(
    --region="${REGION}"
    --project="${PROJECT}"
    --service-account="${SERVICE_ACCOUNT}"
    --ingress=all
    --max-instances=20
    --concurrency=80
    --cpu=1
    --memory=512Mi
    --port=8080
    --timeout=300
    --cpu-boost
    --set-cloudsql-instances="${SQL_INSTANCE}"
)

# Database env shared by both services.
COMMON_ENV="INSTANCE_CONNECTION_NAME=${SQL_INSTANCE},DB_NAME=lpg,DB_USER=postgres"

echo
echo "=== Deploy webhook-handler (public) ==="
gcloud run deploy webhook-handler \
    --image="${FULL_IMAGE}" \
    "${COMMON_FLAGS[@]}" \
    --set-env-vars="${COMMON_ENV}" \
    --set-secrets=SHIFT4_WEBHOOK_TOKEN=shift4-webhook-token:latest \
    --allow-unauthenticated

echo
echo "=== Deploy lpg-admin (IAM-private) ==="
gcloud run deploy lpg-admin \
    --image="${FULL_IMAGE}" \
    "${COMMON_FLAGS[@]}" \
    --set-env-vars="${COMMON_ENV},AZURE_TENANT_ID=${AZURE_TENANT_ID},AZURE_SEND_CLIENT_ID=${AZURE_SEND_CLIENT_ID},CROWN_PO_MAILBOX=${CROWN_PO_MAILBOX},PO_PDF_BUCKET=${PO_PDF_BUCKET}" \
    --set-secrets=AZURE_SEND_CLIENT_SECRET=azure-graph-send-secret:latest \
    --no-allow-unauthenticated

# Assert the admin invoker every deploy (idempotent). --no-allow-unauthenticated
# above guarantees allUsers is not bound; this guarantees the admin principal is.
echo
echo "=== Assert lpg-admin invoker (${ADMIN_INVOKER}) ==="
gcloud run services add-iam-policy-binding lpg-admin \
    --region="${REGION}" \
    --project="${PROJECT}" \
    --member="${ADMIN_INVOKER}" \
    --role=roles/run.invoker \
    --quiet >/dev/null
echo "  ✓ ${ADMIN_INVOKER} -> roles/run.invoker"

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
