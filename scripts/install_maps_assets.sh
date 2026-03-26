#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PREPMASTER_ENV_FILE:-$REPO_ROOT/config/prepmaster.env}"
MAPLIBRE_GL_VERSION="5.17.0"
PMTILES_JS_VERSION="3.2.0"
BASEMAPS_JS_VERSION="5.2.0"
MAPLIBRE_GL_CSS_URL="https://unpkg.com/maplibre-gl@${MAPLIBRE_GL_VERSION}/dist/maplibre-gl.css"
MAPLIBRE_GL_JS_URL="https://unpkg.com/maplibre-gl@${MAPLIBRE_GL_VERSION}/dist/maplibre-gl.js"
PMTILES_JS_URL="https://unpkg.com/pmtiles@${PMTILES_JS_VERSION}/dist/pmtiles.js"
BASEMAPS_JS_URL="https://unpkg.com/@protomaps/basemaps@${BASEMAPS_JS_VERSION}/dist/basemaps.js"
BASEMAPS_ASSETS_ZIP_URL="https://codeload.github.com/protomaps/basemaps-assets/zip/refs/heads/main"

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
install -d -m 0755 "$PREPMASTER_MAPS_ROOT/vendor/maplibre-gl"
install -d -m 0755 "$PREPMASTER_MAPS_ROOT/vendor/pmtiles"
install -d -m 0755 "$PREPMASTER_MAPS_ROOT/vendor/basemaps"
install -d -m 0755 "$PREPMASTER_MAPS_ROOT/assets"
install -d -m 0755 "$PREPMASTER_MAP_PMTILES_ROOT"

tmp_file="$(mktemp)"
if ! curl -fsSL "$MAPLIBRE_GL_CSS_URL" -o "$tmp_file"; then
  rm -f "$tmp_file"
  echo "Failed to download MapLibre CSS from $MAPLIBRE_GL_CSS_URL"
  exit 1
fi
install -m 0644 "$tmp_file" "$PREPMASTER_MAPS_ROOT/vendor/maplibre-gl/maplibre-gl.css"
rm -f "$tmp_file"

tmp_file="$(mktemp)"
if ! curl -fsSL "$MAPLIBRE_GL_JS_URL" -o "$tmp_file"; then
  rm -f "$tmp_file"
  echo "Failed to download MapLibre JS from $MAPLIBRE_GL_JS_URL"
  exit 1
fi
install -m 0644 "$tmp_file" "$PREPMASTER_MAPS_ROOT/vendor/maplibre-gl/maplibre-gl.js"
rm -f "$tmp_file"

tmp_file="$(mktemp)"
if ! curl -fsSL "$PMTILES_JS_URL" -o "$tmp_file"; then
  rm -f "$tmp_file"
  echo "Failed to download PMTiles JS from $PMTILES_JS_URL"
  exit 1
fi
install -m 0644 "$tmp_file" "$PREPMASTER_MAPS_ROOT/vendor/pmtiles/pmtiles.js"
rm -f "$tmp_file"

tmp_file="$(mktemp)"
if ! curl -fsSL "$BASEMAPS_JS_URL" -o "$tmp_file"; then
  rm -f "$tmp_file"
  echo "Failed to download Protomaps basemaps JS from $BASEMAPS_JS_URL"
  exit 1
fi
install -m 0644 "$tmp_file" "$PREPMASTER_MAPS_ROOT/vendor/basemaps/basemaps.js"
rm -f "$tmp_file"

assets_tmp_dir="$(mktemp -d)"
assets_zip="$(mktemp)"
if ! curl -fsSL "$BASEMAPS_ASSETS_ZIP_URL" -o "$assets_zip"; then
  rm -rf "$assets_tmp_dir"
  rm -f "$assets_zip"
  echo "Failed to download basemaps assets archive from $BASEMAPS_ASSETS_ZIP_URL"
  exit 1
fi

unzip -oq "$assets_zip" -d "$assets_tmp_dir"
rm -f "$assets_zip"
rm -rf "$PREPMASTER_MAPS_ROOT/assets/fonts" "$PREPMASTER_MAPS_ROOT/assets/sprites"
cp -R "$assets_tmp_dir"/basemaps-assets-main/fonts "$PREPMASTER_MAPS_ROOT/assets/fonts"
cp -R "$assets_tmp_dir"/basemaps-assets-main/sprites "$PREPMASTER_MAPS_ROOT/assets/sprites"
rm -rf "$assets_tmp_dir"

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
  "maxZoom": ${PREPMASTER_MAP_MAX_ZOOM:-14},
  "glyphsUrl": "/maps/assets/fonts/{fontstack}/{range}.pbf",
  "spriteBaseUrl": "/maps/assets/sprites/v4/${PREPMASTER_MAP_STYLE_FLAVOR:-dark}"
}
EOF

echo "MapLibre assets installed to $PREPMASTER_MAPS_ROOT/vendor/maplibre-gl"
echo "PMTiles assets installed to $PREPMASTER_MAPS_ROOT/vendor/pmtiles"
echo "Protomaps basemap assets installed to $PREPMASTER_MAPS_ROOT/assets"
echo "Offline PMTiles archives should be placed under $PREPMASTER_MAP_PMTILES_ROOT"
echo "Current default archive: $PREPMASTER_MAP_PMTILES_ROOT/${PREPMASTER_MAP_PMTILES_FILE}"
