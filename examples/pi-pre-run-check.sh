#!/bin/sh
# Wrapper you can put in front of a local-model or pi-coding-agent command.
# If the pc-guard status URL is busy or down, the command is not started.
#
#   STATUS_URL=http://127.0.0.1:8743/status \
#     ./examples/pi-pre-run-check.sh pi -p "summarize the README"
#
# For a pi coding agent (pi.dev) or any CLI, this is just a pre-run gate:
# it does not replace the agent's own tools. A Grok Bot can do the same
# check with examples/check-status.py, or via an MCP tool that GETs /status
# (and refuses) before it calls a local model.
set -e
HERE=$(CDPATH='' cd -- "$(dirname "$0")" && pwd)
"$HERE/check-status.sh"
if [ "$#" -eq 0 ]; then
  exit 0
fi
exec "$@"
