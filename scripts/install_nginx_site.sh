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

cat > /etc/nginx/sites-available/prepmaster.conf <<EOF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    root $PREPMASTER_WEB_ROOT;
    index index.html;

    location = / {
        try_files /index.html =404;
    }

    location /admin/ {
        alias $PREPMASTER_ADMIN_ROOT/;
        index index.html;
        try_files \$uri \$uri/ /admin/index.html;
    }

    location /maps/ {
        alias $PREPMASTER_MAPS_ROOT/;
        index index.html;
        try_files \$uri \$uri/ /maps/index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:$ADMIN_PORT/api/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location = /kiwix {
        return 302 http://\$host:$KIWIX_PORT/;
    }

    location /kiwix/ {
        return 302 http://\$host:$KIWIX_PORT/;
    }

    location / {
        try_files \$uri \$uri/ /index.html;
    }
}
EOF

rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/prepmaster.conf /etc/nginx/sites-enabled/prepmaster.conf

nginx -t
systemctl enable nginx
systemctl reload nginx

echo "Nginx site installed and reloaded."
