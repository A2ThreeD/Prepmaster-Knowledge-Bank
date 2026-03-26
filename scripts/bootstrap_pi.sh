#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PREPMASTER_ENV_FILE:-$REPO_ROOT/config/prepmaster.env}"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo $0"
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing config file: $ENV_FILE"
  exit 1
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

PACKAGES=(
  avahi-daemon
  ca-certificates
  curl
  git
  hostapd
  jq
  libjs-leaflet
  nginx
  python3
  python3-pip
  python3-venv
  rsync
  sqlite3
  dnsmasq
  ufw
  unzip
  wget
  xz-utils
)

echo "Updating package lists..."
apt update

echo "Upgrading installed packages..."
apt full-upgrade -y

echo "Installing required packages..."
apt install -y "${PACKAGES[@]}"

echo "Creating project directories..."
install -d -m 0755 "$PREPMASTER_ROOT"
install -d -m 0755 "$PREPMASTER_SRC_DIR"
install -d -m 0755 "$PREPMASTER_WEB_ROOT"
install -d -m 0755 "$PREPMASTER_DATA_ROOT"
install -d -m 0755 "$PREPMASTER_ADMIN_ROOT"
install -d -m 0755 "$PREPMASTER_MAPS_ROOT"
install -d -m 0755 "$KIWIX_LIBRARY_DIR"

echo "Configuring hostname..."
if [[ -n "${PREPMASTER_HOSTNAME:-}" ]]; then
  hostnamectl set-hostname "$PREPMASTER_HOSTNAME"
fi

echo "Enabling core services..."
systemctl enable --now avahi-daemon
systemctl enable nginx

echo "Bootstrap complete."
