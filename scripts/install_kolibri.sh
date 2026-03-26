#!/usr/bin/env bash

set -euo pipefail

source /etc/os-release

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
apt install -y apt-transport-https ca-certificates dirmngr gnupg

gpg --keyserver hkp://keyserver.ubuntu.com:80 \
  --recv-keys DC5BAA93F9E4AE4F0411F97C74F88ADB3194DD81
gpg --output /usr/share/keyrings/learningequality-kolibri.gpg \
  --export DC5BAA93F9E4AE4F0411F97C74F88ADB3194DD81

cat > /etc/apt/sources.list.d/learningequality-ubuntu-kolibri.list <<'EOF'
deb [signed-by=/usr/share/keyrings/learningequality-kolibri.gpg] http://ppa.launchpad.net/learningequality/kolibri/ubuntu jammy main
EOF

echo "Refreshing package metadata for Kolibri..."
apt update

echo "Installing Kolibri..."
DEBIAN_FRONTEND=noninteractive apt install -y kolibri

echo "Enabling Kolibri service..."
systemctl enable kolibri
systemctl restart kolibri || systemctl start kolibri

echo "Kolibri installation complete."
