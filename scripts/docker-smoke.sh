#!/usr/bin/env bash
# Smoke test for the production Docker image.
# Validates that only the intended packages are installed, that the Sandbox
# harness and Playwright are absent, and that the service health endpoints work.
set -euo pipefail

IMAGE="${1:-payment-fee-service:test}"
PORT="${2:-18000}"

echo "==> Docker image smoke test: $IMAGE"

# Required imports
docker run --rm "$IMAGE" python -c "import payment_fee, payment_fee_service; print('production imports ok')"

# Service CLI is available via the installed executable
docker run --rm "$IMAGE" payment-fee-service --help >/dev/null

# Sandbox harness and Playwright must not be importable
if docker run --rm "$IMAGE" python -c "import paypal_sandbox_validation" 2>/dev/null; then
  echo "FAIL: paypal_sandbox_validation is installed but must be absent" >&2
  exit 1
fi
if docker run --rm "$IMAGE" python -c "import playwright" 2>/dev/null; then
  echo "FAIL: playwright is installed but must be absent" >&2
  exit 1
fi

# No credential/artifact paths leaked into the image
docker run --rm "$IMAGE" bash -c '
  for path in /app/paypal-sandbox /app/playwright /app/accounts.csv /app/artifacts/paypal-sandbox; do
    if [ -e "$path" ]; then
      echo "FAIL: unexpected path exists in image: $path" >&2
      exit 1
    fi
  done
'

# Health checks
echo "==> Starting container on port $PORT for health checks"
CID="$(docker run -d --rm -p "127.0.0.1:$PORT:8000" "$IMAGE")"
cleanup() {
  docker stop "$CID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

for i in $(seq 1 30); do
  if curl -fs "http://127.0.0.1:$PORT/health/live" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

curl -fs "http://127.0.0.1:$PORT/health/live"
READY_STATUS="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health/ready")"
echo "health/ready HTTP status: $READY_STATUS"
if [ "$READY_STATUS" != "200" ] && [ "$READY_STATUS" != "503" ]; then
  echo "FAIL: unexpected /health/ready status: $READY_STATUS" >&2
  exit 1
fi

echo "==> Docker smoke test passed"
