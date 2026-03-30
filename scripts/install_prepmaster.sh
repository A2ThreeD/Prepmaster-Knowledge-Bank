#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PREPMASTER_ENV_FILE:-$REPO_ROOT/config/prepmaster.env}"
PROFILE_FILE="${PREPMASTER_PROFILE_FILE:-$REPO_ROOT/config/install-profile.env}"

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

if [[ -f "$PROFILE_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$PROFILE_FILE"
fi

echo "Running base OS bootstrap..."
"$REPO_ROOT/scripts/bootstrap_pi.sh"

echo "Installing framework backup page..."
install -d -m 0755 "$PREPMASTER_WEB_ROOT"
install -d -m 0755 "$PREPMASTER_WEB_ROOT/images"
install -m 0644 "$REPO_ROOT/web/index.html" "$PREPMASTER_WEB_ROOT/index.html"
install -m 0644 "$REPO_ROOT/index.html.framework" "$PREPMASTER_WEB_ROOT/index.html.framework"
if [[ -f "$REPO_ROOT/images/heroimage.png" ]]; then
  install -m 0644 "$REPO_ROOT/images/heroimage.png" "$PREPMASTER_WEB_ROOT/images/heroimage.png"
fi

echo "Installing custom stack placeholders..."
install -d -m 0755 "$PREPMASTER_ADMIN_ROOT"
install -d -m 0755 "$PREPMASTER_MAPS_ROOT"
install -m 0644 "$REPO_ROOT/web/admin/index.html" "$PREPMASTER_ADMIN_ROOT/index.html"
install -m 0644 "$REPO_ROOT/web/maps/index.html" "$PREPMASTER_MAPS_ROOT/index.html"

echo "Installing offline maps assets..."
"$REPO_ROOT/scripts/install_maps_assets.sh"

echo "Preparing Kiwix content directory..."
install -d -m 0755 "$KIWIX_LIBRARY_DIR"

echo "Generating default Kiwix manifest..."
python3 "$REPO_ROOT/scripts/build_kiwix_zim_manifest.py" \
  --source "$REPO_ROOT/catalog/kiwix-categories.yaml" \
  --output "$REPO_ROOT/config/kiwix-zim-urls.txt" \
  --profile "${PREPMASTER_ZIM_PROFILE:-essential}" \
  --wikipedia-options "$REPO_ROOT/catalog/wikipedia.yaml" \
  --wikipedia-choice "${PREPMASTER_WIKIPEDIA_OPTION:-top-mini}"

echo "Installing portal API service..."
"$REPO_ROOT/scripts/install_portal_service.sh"

echo "Installing Kiwix service..."
"$REPO_ROOT/scripts/install_kiwix_service.sh"

echo "Installing Nginx site..."
"$REPO_ROOT/scripts/install_nginx_site.sh"

echo "Selected install profile:"
echo "  Base stack: ${INSTALL_BASE_STACK:-1}"
echo "  Kiwix: ${INSTALL_KIWIX:-1}"
echo "  OpenStreetMaps: ${INSTALL_OPENSTREETMAPS:-1}"
echo "  Wikipedia option: ${PREPMASTER_WIKIPEDIA_OPTION:-top-mini}"
echo "  Kolibri: ${INSTALL_KOLIBRI:-0}"
echo "  KA Lite: ${INSTALL_KA_LITE:-0}"

echo "Installing optional components from profile..."
"$REPO_ROOT/scripts/install_optional_components.sh"

if [[ "${PREPMASTER_AP_ENABLED:-0}" == "1" ]]; then
  echo "Applying wireless access point configuration..."
  "$REPO_ROOT/scripts/configure_access_point.sh"
else
  echo "Wireless access point mode not enabled."
fi

echo "SOPR base installation complete."
echo
echo "Suggested next steps:"
echo "  1. sudo $REPO_ROOT/scripts/download_kiwix_zims.sh"
echo "  2. Visit http://$PREPMASTER_HOSTNAME.local/ or the Pi IP address on your local network"
echo "  3. Open /admin if you want to revisit setup preferences"
