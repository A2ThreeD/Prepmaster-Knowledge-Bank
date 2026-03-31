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

if ! command -v kiwix-manage >/dev/null 2>&1; then
  echo "kiwix-manage is not installed. Install kiwix-tools first."
  exit 1
fi

install -d -m 0755 "$(dirname "$KIWIX_LIBRARY_XML")"
rm -f "$KIWIX_LIBRARY_XML"

mapfile -t zims < <(find -L "$KIWIX_LIBRARY_DIR" -maxdepth 1 -type f -name '*.zim' | sort)

if [[ ${#zims[@]} -eq 0 ]]; then
  echo "No ZIM files found in $KIWIX_LIBRARY_DIR"
  exit 0
fi

for zim in "${zims[@]}"; do
  echo "Adding to library: $zim"
  kiwix-manage "$KIWIX_LIBRARY_XML" add "$zim"
done

echo "Kiwix library rebuilt at $KIWIX_LIBRARY_XML"
