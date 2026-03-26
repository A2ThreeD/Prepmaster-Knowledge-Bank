#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PREPMASTER_ENV_FILE:-$REPO_ROOT/config/prepmaster.env}"
LEAFLET_SOURCE_DIR="/usr/share/javascript/leaflet"

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

install -d -m 0755 "$PREPMASTER_MAPS_ROOT"
install -d -m 0755 "$PREPMASTER_MAPS_ROOT/vendor"
install -d -m 0755 "$PREPMASTER_MAPS_ROOT/vendor/leaflet"
install -d -m 0755 "$PREPMASTER_MAP_TILE_ROOT"

if [[ ! -d "$LEAFLET_SOURCE_DIR" ]]; then
  echo "Missing Leaflet assets in $LEAFLET_SOURCE_DIR"
  echo "Install the libjs-leaflet package first."
  exit 1
fi

install -m 0644 "$LEAFLET_SOURCE_DIR/leaflet.css" "$PREPMASTER_MAPS_ROOT/vendor/leaflet/leaflet.css"
install -m 0644 "$LEAFLET_SOURCE_DIR/leaflet.js" "$PREPMASTER_MAPS_ROOT/vendor/leaflet/leaflet.js"

if [[ -d "$LEAFLET_SOURCE_DIR/images" ]]; then
  install -d -m 0755 "$PREPMASTER_MAPS_ROOT/vendor/leaflet/images"
  cp -R "$LEAFLET_SOURCE_DIR/images/." "$PREPMASTER_MAPS_ROOT/vendor/leaflet/images/"
fi

echo "Leaflet assets installed to $PREPMASTER_MAPS_ROOT/vendor/leaflet"
echo "Offline map tiles should be placed under $PREPMASTER_MAP_TILE_ROOT using the layout z/x/y.png"
