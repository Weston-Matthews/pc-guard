#!/bin/sh
# Example unload hook. Copy and point unload_hook at your copy.
# The watcher calls this with one or more model names as arguments.
#
# Default behavior in pc-guard is `ollama stop <model>`. Use a hook if
# you run llama.cpp (or anything else) instead. This example does not
# kill processes; it only tries a clean shutdown where one exists.
set -e
for name in "$@"; do
  case $name in
    llama-server|*.gguf)
      # llama-server has no standard unload API. Stop the user service
      # you use to run it, or leave this as a no-op and just refuse new jobs.
      # systemctl --user stop llama-server.service
      echo "no automatic unload for $name; stop the server yourself or edit this hook" >&2
      ;;
    *)
      if command -v ollama >/dev/null 2>&1; then
        ollama stop "$name" || true
      fi
      ;;
  esac
done
