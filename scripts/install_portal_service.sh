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

install -d -m 0755 "$PREPMASTER_ROOT/app"
install -d -m 0755 "$PREPMASTER_DATA_ROOT"
install -m 0755 "$REPO_ROOT/app/prepmaster_portal.py" "$PREPMASTER_ROOT/app/prepmaster_portal.py"

cat > /etc/systemd/system/prepmaster-portal.service <<EOF
[Unit]
Description=Prepmaster portal API
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/env python3 $PREPMASTER_ROOT/app/prepmaster_portal.py --host 127.0.0.1 --repo-root "$REPO_ROOT" --data-dir "$PREPMASTER_DATA_ROOT" --port "$ADMIN_PORT"
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable prepmaster-portal.service
systemctl restart prepmaster-portal.service || systemctl start prepmaster-portal.service

echo "Prepmaster portal API installed on port $ADMIN_PORT"
