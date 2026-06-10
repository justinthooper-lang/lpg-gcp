#!/usr/bin/env bash
#
# Build and deploy the crown-invoice-sync Cloud Run JOB.
#
# Usage:
#   ./scripts/deploy-job.sh v0.12.6
#
# Separate from deploy.sh (which handles the webhook SERVICES) because the
# job has a different lifecycle, deploy verb, and verification model -- and
# the two-image split means the job and services release independently.
#
# This script declares the job's FULL configuration on every deploy, so the
# script is the single source of truth for the job's shape (image, identity,
# secret, env vars, Cloud SQL, limits). "jobs deploy" is create-or-update,
# so this works on a fresh environment and on redeploys alike.
#
# After deploy it executes the job once (--wait) as a smoke test. NOTE: this
# runs the real sync against the live mailbox + DB. It is idempotent (Crown's
# duplicate emails and re-syncs are skipped), so re-running is safe, but it
# does real work -- not a free health check.
set -euo pipefail

REGION="us-west1"
PROJECT="lpg-dev-496820"
JOB_NAME="crown-invoice-sync"
IMAGE_REPO="us-west1-docker.pkg.dev/${PROJECT}/lpg-images/crown-sync"
SERVICE_ACCOUNT="crown-sync-job@${PROJECT}.iam.gserviceaccount.com"
SQL_INSTANCE="${PROJECT}:${REGION}:lpg-dev"
IAM_DB_USER="crown-sync-job@${PROJECT}.iam"

AZURE_TENANT_ID="fa215d01-a503-4496-ae9f-3ab71e89037e"
AZURE_CLIENT_ID="c36883bf-a1b7-4e63-8fc1-c965b32d76ce"
TARGET_MAILBOX="customerservice@lamppostglobes.com"

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <version-tag>" >&2
    echo "Example: $0 v0.12.6" >&2
    exit 1
fi
VERSION="$1"
if [[ ! "${VERSION}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: version must look like vX.Y.Z (got: ${VERSION})" >&2
    exit 1
fi
FULL_IMAGE="${IMAGE_REPO}:${VERSION}"

cd "$(dirname "$0")/.."

echo "=== Build ==="
echo "Image: ${FULL_IMAGE}"
gcloud builds submit \
    --config cloudbuild.crownsync.yaml \
    --substitutions=_TAG="${VERSION}" \
    .

echo
echo "=== Deploy job: ${JOB_NAME} ==="
gcloud run jobs deploy "${JOB_NAME}" \
    --image="${FULL_IMAGE}" \
    --region="${REGION}" \
    --project="${PROJECT}" \
    --service-account="${SERVICE_ACCOUNT}" \
    --set-cloudsql-instances="${SQL_INSTANCE}" \
    --set-secrets=AZURE_CLIENT_SECRET=azure-graph-client-secret:latest \
    --set-env-vars=AZURE_TENANT_ID="${AZURE_TENANT_ID}" \
    --set-env-vars=AZURE_CLIENT_ID="${AZURE_CLIENT_ID}" \
    --set-env-vars=TARGET_MAILBOX="${TARGET_MAILBOX}" \
    --set-env-vars=INSTANCE_CONNECTION_NAME="${SQL_INSTANCE}" \
    --set-env-vars=DB_NAME=lpg \
    --set-env-vars=IAM_DB_USER="${IAM_DB_USER}" \
    --max-retries=1 \
    --task-timeout=10m

echo
echo "=== Smoke test: execute once ==="
echo "(runs the real sync; idempotent, safe to re-run)"
gcloud run jobs execute "${JOB_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT}" \
    --wait

echo
echo "=== Done ==="
echo "Job ${JOB_NAME} deployed at ${VERSION} and executed successfully."