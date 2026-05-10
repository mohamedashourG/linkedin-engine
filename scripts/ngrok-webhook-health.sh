#!/usr/bin/env bash
# Start ngrok → localhost (default 8000), print public HTTPS URL, curl /healthz.
# Requires a free ngrok account: https://dashboard.ngrok.com/get-started/your-authtoken
#
# Usage:
#   export NGROK_AUTHTOKEN="your_token"
#   ./scripts/ngrok-webhook-health.sh
# Or add NGROK_AUTHTOKEN to .env and:
#   set -a && source .env && set +a && ./scripts/ngrok-webhook-health.sh

set -euo pipefail
PORT="${1:-8000}"

if [[ -z "${NGROK_AUTHTOKEN:-}" ]]; then
  echo "NGROK_AUTHTOKEN is not set."
  echo "Get a token: https://dashboard.ngrok.com/get-started/your-authtoken"
  echo "Then: export NGROK_AUTHTOKEN='…' && $0"
  exit 1
fi
export NGROK_AUTHTOKEN

if ! curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
  echo "WARNING: nothing responded at http://127.0.0.1:${PORT}/healthz — start the API first."
fi

ngrok http "$PORT" --log=stdout >/tmp/ngrok-agent.log 2>&1 &
NG_PID=$!
cleanup() { kill "$NG_PID" 2>/dev/null || true; }
trap cleanup EXIT

PUBLIC_URL=""
for _ in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:4040/api/tunnels >/tmp/ngrok-tunnels.json 2>/dev/null; then
    PUBLIC_URL="$(python3 -c "
import json
with open('/tmp/ngrok-tunnels.json') as f:
    j = json.load(f)
for t in j.get('tunnels') or []:
    u = (t.get('public_url') or '')
    if u.startswith('https://'):
        print(u)
        break
" 2>/dev/null || true)"
    if [[ -n "$PUBLIC_URL" ]]; then
      break
    fi
  fi
  sleep 0.25
done

if [[ -z "$PUBLIC_URL" ]]; then
  echo "Could not read ngrok public URL from http://127.0.0.1:4040/api/tunnels"
  echo "Last agent log:"
  tail -20 /tmp/ngrok-agent.log || true
  exit 1
fi

echo "Public URL: $PUBLIC_URL"
echo "Set in .env: CRUSTDATA_WEBHOOK_BASE_URL=$PUBLIC_URL"
echo ""
echo "Health via tunnel:"
HC="$(curl -sS -o /tmp/ngrok-health.body -w "%{http_code}" "${PUBLIC_URL}/healthz" || echo "000")"
echo "HTTP $HC"
cat /tmp/ngrok-health.body
echo ""

if [[ "$HC" != "200" ]]; then
  exit 1
fi

echo "OK"
if [[ "${KEEP_NGROK:-}" == "1" ]]; then
  echo "KEEP_NGROK=1 — leaving ngrok running (pid $NG_PID). Stop with: kill $NG_PID"
  trap - EXIT
  wait "$NG_PID"
else
  echo "ngrok stopped. For a long-lived tunnel: ngrok http $PORT"
fi
