#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PREPMASTER_ENV_FILE:-${SOPR_ENV_FILE:-$REPO_ROOT/config/sopr.env}}"
if [[ ! -f "$ENV_FILE" && -f "$REPO_ROOT/config/prepmaster.env" ]]; then
  ENV_FILE="$REPO_ROOT/config/prepmaster.env"
fi

source /etc/os-release

if [[ -f "$ENV_FILE" ]]; then
# shellcheck source=./load_env.sh
source "$REPO_ROOT/scripts/load_env.sh"
load_sopr_env "$ENV_FILE"
fi

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo $0"
  exit 1
fi

if [[ "${ID:-}" != "debian" && "${ID:-}" != "raspbian" && "${ID_LIKE:-}" != *"debian"* ]]; then
  echo "Unsupported OS for this Kolibri installer: ${PRETTY_NAME:-unknown}"
  exit 1
fi

if [[ "${VERSION_ID:-0}" != "12" && "${VERSION_ID:-0}" != "13" ]]; then
  echo "This automated Kolibri installer currently targets Debian 12+ / current Raspberry Pi OS releases."
  echo "Detected: ${PRETTY_NAME:-unknown}"
  exit 1
fi

echo "Installing Kolibri prerequisites..."
apt install -y ca-certificates curl dirmngr gnupg

LEGACY_KOLIBRI_LIST="/etc/apt/sources.list.d/learningequality-ubuntu-kolibri.list"
LEGACY_KOLIBRI_KEYRING="/usr/share/keyrings/learningequality-kolibri.gpg"
KOLIBRI_PORT="${PREPMASTER_KOLIBRI_PORT:-8082}"

# Debian trixie (13) rejects the old Launchpad PPA signing path, so clear any
# stale repo entry first to keep apt healthy.
rm -f "$LEGACY_KOLIBRI_LIST"

install_from_deb() {
  local tmp_deb
  tmp_deb="$(mktemp /tmp/kolibri-installer-XXXXXX.deb)"
  trap 'rm -f "$tmp_deb"' EXIT

  echo "Downloading Kolibri .deb installer..."
  curl -L --fail --output "$tmp_deb" "https://learningequality.org/r/kolibri-deb-latest"

  echo "Installing Kolibri from downloaded .deb..."
  DEBIAN_FRONTEND=noninteractive apt install -y "$tmp_deb"

  rm -f "$tmp_deb"
  trap - EXIT
}

install_from_ppa() {
  gpg --keyserver hkp://keyserver.ubuntu.com:80 \
    --recv-keys DC5BAA93F9E4AE4F0411F97C74F88ADB3194DD81
  gpg --output "$LEGACY_KOLIBRI_KEYRING" \
    --export DC5BAA93F9E4AE4F0411F97C74F88ADB3194DD81

  cat > "$LEGACY_KOLIBRI_LIST" <<'EOF'
deb [signed-by=/usr/share/keyrings/learningequality-kolibri.gpg] http://ppa.launchpad.net/learningequality/kolibri/ubuntu jammy main
EOF

  echo "Refreshing package metadata for Kolibri..."
  apt update

  echo "Installing Kolibri from PPA..."
  DEBIAN_FRONTEND=noninteractive apt install -y kolibri
}

if [[ "${VERSION_ID:-0}" == "13" ]]; then
  install_from_deb
else
  if ! install_from_ppa; then
    echo "Kolibri PPA install failed; falling back to the official .deb installer..."
    rm -f "$LEGACY_KOLIBRI_LIST"
    install_from_deb
  fi
fi

install -d -m 0755 /etc/kolibri/conf.d
cat > /etc/kolibri/conf.d/sopr.conf <<EOF
KOLIBRI_LISTEN_PORT="$KOLIBRI_PORT"
EOF

echo "Stopping any stale Kolibri runtime before service startup..."
runuser "${KOLIBRI_USER:-prepper}" -c "kolibri stop" >/dev/null 2>&1 || true

echo "Enabling Kolibri service..."
systemctl enable kolibri
systemctl restart kolibri || systemctl start kolibri

echo "Kolibri installation complete."
