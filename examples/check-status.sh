#!/bin/sh
# Exit 0 if the PC is not busy and a status endpoint answered.
# Exit 2 if busy, 3 if unreachable. Use before starting a local model job.
#
#   STATUS_URL=http://127.0.0.1:8743/status ./examples/check-status.sh
set -e
URL=${STATUS_URL:-http://127.0.0.1:8743/status}
# python3 is used to parse JSON portably without jq.
resp=$(curl -sS --max-time 3 "$URL") || {
  echo "pc-guard: status unreachable at $URL" >&2
  exit 3
}
python3 -c '
import json, sys
data = json.loads(sys.argv[1])
if data.get("busy"):
    reasons = ", ".join(data.get("reasons") or []) or "unknown"
    print("pc-guard: busy (%s)" % reasons, file=sys.stderr)
    sys.exit(2)
print("pc-guard: not busy (idle_seconds=%s)" % data.get("idle_seconds"))
' "$resp"
