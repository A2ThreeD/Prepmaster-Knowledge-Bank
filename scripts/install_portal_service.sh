#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PREPMASTER_ENV_FILE:-${SOPR_ENV_FILE:-$REPO_ROOT/config/sopr.env}}"
if [[ ! -f "$ENV_FILE" && -f "$REPO_ROOT/config/prepmaster.env" ]]; then
  ENV_FILE="$REPO_ROOT/config/prepmaster.env"
fi

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo $0"
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing config file: $ENV_FILE"
  exit 1
fi

# shellcheck source=./load_env.sh
source "$REPO_ROOT/scripts/load_env.sh"
load_sopr_env "$ENV_FILE"

install -d -m 0755 "$PREPMASTER_ROOT/app"
install -d -m 0755 "$PREPMASTER_DATA_ROOT"
install -m 0755 "$REPO_ROOT/app/sopr_portal.py" "$PREPMASTER_ROOT/app/sopr_portal.py"
ln -sfn "$PREPMASTER_ROOT/app/sopr_portal.py" "$PREPMASTER_ROOT/app/prepmaster_portal.py"

cat > /etc/systemd/system/sopr-portal.service <<EOF
[Unit]
Description=SOPR portal API
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/env python3 $PREPMASTER_ROOT/app/sopr_portal.py --host 127.0.0.1 --repo-root "$REPO_ROOT" --data-dir "$PREPMASTER_DATA_ROOT" --port "$ADMIN_PORT"
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable sopr-portal.service
ln -sfn /etc/systemd/system/sopr-portal.service /etc/systemd/system/prepmaster-portal.service
systemctl restart sopr-portal.service || systemctl start sopr-portal.service

echo "SOPR portal API installed on port $ADMIN_PORT"
