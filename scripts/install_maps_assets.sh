#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PREPMASTER_ENV_FILE:-$REPO_ROOT/config/prepmaster.env}"
LEAFLET_SOURCE_DIR="/usr/share/javascript/leaflet"
PROTOMAPS_LEAFLET_VERSION="5.0.0"
PROTOMAPS_LEAFLET_URL="https://unpkg.com/protomaps-leaflet@${PROTOMAPS_LEAFLET_VERSION}/dist/protomaps-leaflet.js"

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
install -d -m 0755 "$PREPMASTER_MAPS_ROOT/vendor/protomaps-leaflet"
install -d -m 0755 "$PREPMASTER_MAP_PMTILES_ROOT"

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

tmp_file="$(mktemp)"
if ! curl -fsSL "$PROTOMAPS_LEAFLET_URL" -o "$tmp_file"; then
  rm -f "$tmp_file"
  echo "Failed to download Protomaps Leaflet assets from $PROTOMAPS_LEAFLET_URL"
  exit 1
fi

install -m 0644 "$tmp_file" "$PREPMASTER_MAPS_ROOT/vendor/protomaps-leaflet/protomaps-leaflet.js"
rm -f "$tmp_file"

cat > "$PREPMASTER_MAPS_ROOT/config.json" <<EOF
{
  "pmtilesUrl": "/pmtiles/${PREPMASTER_MAP_PMTILES_FILE}",
  "pmtilesFile": "${PREPMASTER_MAP_PMTILES_FILE}",
  "flavor": "${PREPMASTER_MAP_STYLE_FLAVOR:-dark}",
  "language": "${PREPMASTER_MAP_LANGUAGE:-en}",
  "defaultLat": ${PREPMASTER_MAP_DEFAULT_LAT:-39.8283},
  "defaultLon": ${PREPMASTER_MAP_DEFAULT_LON:--98.5795},
  "defaultZoom": ${PREPMASTER_MAP_DEFAULT_ZOOM:-4},
  "minZoom": ${PREPMASTER_MAP_MIN_ZOOM:-2},
  "maxZoom": ${PREPMASTER_MAP_MAX_ZOOM:-14}
}
EOF

echo "Leaflet assets installed to $PREPMASTER_MAPS_ROOT/vendor/leaflet"
echo "Protomaps Leaflet assets installed to $PREPMASTER_MAPS_ROOT/vendor/protomaps-leaflet"
echo "Offline PMTiles archives should be placed under $PREPMASTER_MAP_PMTILES_ROOT"
echo "Current default archive: $PREPMASTER_MAP_PMTILES_ROOT/${PREPMASTER_MAP_PMTILES_FILE}"
