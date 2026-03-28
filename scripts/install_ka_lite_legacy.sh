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

if [[ "${PREPMASTER_ALLOW_LEGACY_KA_LITE:-0}" != "1" ]]; then
  echo "KA Lite installation is blocked by default."
  echo "Set PREPMASTER_ALLOW_LEGACY_KA_LITE=1 in config/prepmaster.env to acknowledge the legacy risk."
  exit 1
fi

echo "KA Lite is a legacy platform with outdated installation requirements."
echo "This script currently records the request but does not attempt a full unattended install on modern Raspberry Pi OS."
echo "Recommended path: use Kolibri unless you specifically need KA Lite compatibility."

install -d -m 0755 /opt/prepmaster/legacy
cat > /opt/prepmaster/legacy/ka-lite-requested.txt <<'EOF'
KA Lite was requested for this SOPR deployment.
The install was intentionally not automated because KA Lite is no longer actively developed
and its supported Linux installation path depends on legacy tooling that is not reliable on
current Raspberry Pi OS releases.
EOF

echo "Legacy KA Lite request recorded at /opt/prepmaster/legacy/ka-lite-requested.txt"
