#!/bin/sh
# Install and start the user systemd services. Does not need sudo.
# Run from a clone, or pass the install directory as the first argument.
set -e

REPO_DIR=$(CDPATH='' cd -- "$(dirname "$0")/.." && pwd)
INSTALL_DIR=${1:-$REPO_DIR}
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pc-guard"

if [ ! -f "$INSTALL_DIR/pc_guard.py" ]; then
  echo "pc_guard.py not found in $INSTALL_DIR" >&2
  exit 1
fi

if [ ! -x "$INSTALL_DIR/idle_helper" ]; then
  echo "idle_helper is not built. On a KDE system run: make -C $INSTALL_DIR" >&2
  echo "Continuing; the idle service will fail until it is built (X11/GNOME fallbacks may still work)." >&2
fi

mkdir -p "$UNIT_DIR" "$CONF_DIR"
if [ ! -f "$INSTALL_DIR/pc-guard.conf" ] && [ ! -f "$CONF_DIR/pc-guard.conf" ]; then
  cp "$INSTALL_DIR/pc-guard.conf.example" "$CONF_DIR/pc-guard.conf"
  echo "Wrote $CONF_DIR/pc-guard.conf from the example. Edit it before relying on defaults."
fi

PLATFORM_LINE=""
if [ -n "${WAYLAND_DISPLAY:-}" ]; then
  PLATFORM_LINE="Environment=QT_QPA_PLATFORM=wayland"
fi

# Substitute the install path into the unit templates.
for unit in pc-guard-idle pc-guard-status pc-guard-watcher; do
  src="$INSTALL_DIR/systemd/${unit}.service.in"
  if [ ! -f "$src" ]; then
    echo "missing $src" >&2
    exit 1
  fi
  sed -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
      -e "s|@QT_PLATFORM_LINE@|$PLATFORM_LINE|g" \
      "$src" > "$UNIT_DIR/${unit}.service"
done

# Optional on-demand llama.cpp unit: installed, never enabled. pc-guard
# starts it when llamacpp_unit=llama-server.service is set in pc-guard.conf.
if [ -f "$INSTALL_DIR/systemd/llama-server.service.in" ] && [ ! -e "$UNIT_DIR/llama-server.service" ]; then
  sed -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" "$INSTALL_DIR/systemd/llama-server.service.in" > "$UNIT_DIR/llama-server.service"
  echo "Installed $UNIT_DIR/llama-server.service (not enabled). See examples/llama-server.env.example."
fi

systemctl --user daemon-reload
systemctl --user enable --now pc-guard-idle.service pc-guard-status.service pc-guard-watcher.service
systemctl --user --no-pager --full status pc-guard-idle.service pc-guard-status.service pc-guard-watcher.service || true
echo "User services installed and started from $INSTALL_DIR"
echo "Optional root setup (firewall, linger, Wake-on-LAN check):"
echo "  sudo $INSTALL_DIR/scripts/sudo-setup.sh"
