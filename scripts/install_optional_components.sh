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

if [[ "${INSTALL_KOLIBRI:-0}" == "1" ]]; then
  "$REPO_ROOT/scripts/install_kolibri.sh"
else
  echo "Kolibri not selected."
fi

if [[ "${INSTALL_KA_LITE:-0}" == "1" ]]; then
  "$REPO_ROOT/scripts/install_ka_lite_legacy.sh"
else
  echo "KA Lite not selected."
fi
