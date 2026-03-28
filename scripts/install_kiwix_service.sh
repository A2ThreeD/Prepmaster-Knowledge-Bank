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

echo "Installing Kiwix tools..."
apt install -y kiwix-tools

install -d -m 0755 "$(dirname "$KIWIX_LIBRARY_XML")"
install -d -m 0755 "$PREPMASTER_ROOT/app"
install -d -m 0755 "$PREPMASTER_WEB_ROOT/kiwix-placeholder"
install -m 0755 "$REPO_ROOT/scripts/run_kiwix_service.sh" "$PREPMASTER_ROOT/app/run_kiwix_service.sh"
install -m 0644 "$REPO_ROOT/web/kiwix-placeholder/index.html" "$PREPMASTER_WEB_ROOT/kiwix-placeholder/index.html"

cat > /etc/systemd/system/prepmaster-kiwix.service <<EOF
[Unit]
Description=SOPR Kiwix Service
After=network.target

[Service]
Type=simple
Environment=PREPMASTER_ENV_FILE=$ENV_FILE
ExecStart=$PREPMASTER_ROOT/app/run_kiwix_service.sh
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable prepmaster-kiwix.service

if [[ -d "$KIWIX_LIBRARY_DIR" ]]; then
  "$REPO_ROOT/scripts/rebuild_kiwix_library.sh" || true
fi

systemctl restart prepmaster-kiwix.service || systemctl start prepmaster-kiwix.service

echo "Kiwix service installed on port $KIWIX_PORT"
