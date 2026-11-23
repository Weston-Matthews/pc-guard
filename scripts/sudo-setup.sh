#!/bin/sh
# Commands that need root. Safe to re-run.
#   sudo scripts/sudo-setup.sh
#
# Reads optional settings from PC_GUARD_* environment variables or from
# pc-guard.conf in the repo or in ~/.config/pc-guard/.
set -e

REPO_DIR=$(CDPATH='' cd -- "$(dirname "$0")/.." && pwd)
# When run under sudo, prefer the invoking user's home for the config.
USER_HOME=${SUDO_USER:+$(getent passwd "$SUDO_USER" | cut -d: -f6)}
USER_HOME=${USER_HOME:-$HOME}
TARGET_USER=${SUDO_USER:-${USER:-}}

conf_get() {
  key=$1
  env_name=PC_GUARD_$(echo "$key" | tr '[:lower:]' '[:upper:]')
  eval "val=\${$env_name:-}"
  if [ -n "$val" ]; then
    printf '%s\n' "$val"
    return 0
  fi
  for f in \
      ${PC_GUARD_CONFIG:-} \
      "$REPO_DIR/pc-guard.conf" \
      "$USER_HOME/.config/pc-guard/pc-guard.conf"
  do
    [ -n "$f" ] && [ -f "$f" ] || continue
    while IFS= read -r line || [ -n "$line" ]; do
      case $line in
        ""|\#*) continue ;;
      esac
      k=${line%%=*}
      v=${line#*=}
      if [ "$k" = "$key" ]; then
        printf '%s\n' "$v"
        return 0
      fi
    done < "$f"
  done
  return 0
}

PORT=$(conf_get status_port)
PORT=${PORT:-8743}
WOL_IFACE=$(conf_get wol_interface)

detect_iface() {
  if [ -n "$WOL_IFACE" ]; then
    printf '%s\n' "$WOL_IFACE"
    return
  fi
  # Prefer a wired interface that is up, excluding loopback, wireless, and tailscale.
  for path in /sys/class/net/*; do
    name=$(basename "$path")
    case $name in
      lo|tailscale*|docker*|veth*|virbr*|br*|wl*) continue ;;
    esac
    if [ -d "$path/device" ] && [ ! -d "$path/wireless" ]; then
      printf '%s\n' "$name"
      return
    fi
  done
}

IFACE=$(detect_iface)

echo "=== Wake-on-LAN ==="
if [ -z "$IFACE" ]; then
  echo "No ethernet interface found. Set wol_interface in pc-guard.conf and re-run."
elif ! command -v ethtool >/dev/null 2>&1; then
  echo "ethtool is not installed; skipping the Wake-on-LAN check for $IFACE"
else
  echo "Interface: $IFACE"
  ethtool "$IFACE" | grep -i -E "Wake-on|Supports Wake" || true
  if ethtool "$IFACE" | grep -q "Wake-on: g"; then
    echo "Wake-on is already g"
  else
    echo "Setting wol g on $IFACE for this boot"
    ethtool -s "$IFACE" wol g || echo "Could not set wol g (some NICs or drivers refuse it)."
    ethtool "$IFACE" | grep -i -E "Wake-on" || true
  fi
  echo "If this does not persist across connect/reboot, enable magic-packet wake"
  echo "in your network manager and in the firmware/BIOS."
fi

echo
echo "=== Firewall: allow status port $PORT on tailscale0 only ==="
if command -v ufw >/dev/null 2>&1; then
  if ufw status | grep -q "$PORT"; then
    echo "A $PORT rule already exists:"
    ufw status | grep "$PORT" || true
  else
    ufw allow in on tailscale0 to any port "$PORT" proto tcp comment "pc-guard status Tailscale only"
    echo "Added $PORT rule on tailscale0"
  fi
  ufw status verbose | grep -E "Status|$PORT|tailscale" || ufw status verbose
elif command -v firewall-cmd >/dev/null 2>&1; then
  echo "firewalld is present. Example (not applied automatically):"
  echo "  firewall-cmd --permanent --zone=trusted --add-port=${PORT}/tcp"
  echo "  # or a rich rule limited to the tailscale0 interface, then:"
  echo "  firewall-cmd --reload"
else
  echo "No ufw or firewalld found. Add a filter that allows TCP $PORT only on"
  echo "tailscale0 (or your LAN interface), and denies it on public interfaces."
fi

echo
echo "=== linger (user services after reboot, before login) ==="
if [ -z "$TARGET_USER" ]; then
  echo "Could not determine the target user. Run: loginctl enable-linger <username>"
else
  loginctl enable-linger "$TARGET_USER"
  loginctl show-user "$TARGET_USER" -p Linger
fi

echo
echo "Done."
echo
echo "Optional, not done by this script: if you want to share Ollama or"
echo "llama-server over the tailnet, bind that service to the Tailscale"
echo "address (not 0.0.0.0 on the public internet) and restrict the port"
echo "with the same interface-scoped firewall rule."
