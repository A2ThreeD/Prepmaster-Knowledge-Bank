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

    location /pmtiles/ {
        alias $PREPMASTER_MAP_PMTILES_ROOT/;
        default_type application/octet-stream;
        access_log off;
        expires 7d;
        add_header Cache-Control "public";
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
        return 302 /kiwix/;
    }

    location ^~ /kiwix/ {
        proxy_pass http://127.0.0.1:$KIWIX_PORT/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_redirect ~^http://[^/]+:$KIWIX_PORT/(.*)$ /kiwix/\$1;
    }

    location ^~ /skin/ {
        proxy_pass http://127.0.0.1:$KIWIX_PORT/skin/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location ^~ /catalog/ {
        proxy_pass http://127.0.0.1:$KIWIX_PORT/catalog/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location = /viewer_settings.js {
        proxy_pass http://127.0.0.1:$KIWIX_PORT/viewer_settings.js;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location ^~ /content/ {
        proxy_pass http://127.0.0.1:$KIWIX_PORT/content/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location ^~ /raw/ {
        proxy_pass http://127.0.0.1:$KIWIX_PORT/raw/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location = /nojs {
        proxy_pass http://127.0.0.1:$KIWIX_PORT/nojs;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
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
