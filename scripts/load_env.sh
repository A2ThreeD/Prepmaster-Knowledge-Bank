#!/usr/bin/env bash

mirror_sopr_prefixed_vars() {
  local suffix old_key new_key old_val new_val
  for suffix in \
    ADMIN_ROOT \
    ALLOW_LEGACY_KA_LITE \
    AP_ADDRESS \
    AP_CHANNEL \
    AP_CIDR \
    AP_COUNTRY \
    AP_DHCP_END \
    AP_DHCP_LEASE \
    AP_DHCP_START \
    AP_ENABLED \
    AP_INTERFACE \
    AP_LEASE \
    AP_NETMASK \
    AP_PASSPHRASE \
    AP_SSID \
    CONTENT_TAG_CSV \
    DATA_ROOT \
    ENV_FILE \
    HOSTNAME \
    HOST_FALLBACK_IP \
    HOST_FALLBACK_NAME \
    HOST_IP \
    HOST_IP_LIST \
    HTTP_PORT \
    INSTALL_KA_LITE \
    INSTALL_KIWIX \
    INSTALL_KOLIBRI \
    INSTALL_MAPS \
    KOLIBRI_PORT \
    MAPS_ROOT \
    MAP_CATALOG_URL \
    MAP_COLLECTIONS \
    MAP_COLLECTIONS_URL \
    MAP_DEFAULT_LAT \
    MAP_DEFAULT_LON \
    MAP_DEFAULT_ZOOM \
    MAP_INSTALL_DIR \
    MAP_LANGUAGE \
    MAP_MAX_ZOOM \
    MAP_MIN_ZOOM \
    MAP_PMTILES_FILE \
    MAP_PMTILES_ROOT \
    MAP_REPO_BRANCH \
    MAP_REPO_NAME \
    MAP_REPO_OWNER \
    MAP_REPO_SUBDIR \
    MAP_SELECTED_COLLECTIONS \
    MAP_SELECTED_FILES \
    MAP_STYLE_FLAVOR \
    PROFILE_FILE \
    ROOT \
    SRC_DIR \
    WEB_ROOT \
    WIKIPEDIA_INSTALL_DIR \
    WIKIPEDIA_OPTION \
    ZIM_CATALOG_URL \
    ZIM_CUSTOM_BASE_PROFILE \
    ZIM_CUSTOM_SELECTION_FILE \
    ZIM_CUSTOM_URL_FILE \
    ZIM_INSTALL_DIR \
    ZIM_MODE \
    ZIM_PROFILE \
    ZIM_URL_FILE
  do
    old_key="PREPMASTER_${suffix}"
    new_key="SOPR_${suffix}"
    old_val="${!old_key-}"
    new_val="${!new_key-}"
    if [[ -n "$new_val" && -z "$old_val" ]]; then
      printf -v "$old_key" '%s' "$new_val"
      export "$old_key"
      continue
    fi
    if [[ -n "$old_val" && -z "$new_val" ]]; then
      printf -v "$new_key" '%s' "$old_val"
      export "$new_key"
    fi
  done
}

load_sopr_env() {
  local env_file="${1:?missing env file path}"
  # shellcheck disable=SC1090
  source "$env_file"
  mirror_sopr_prefixed_vars
  export SOPR_ENV_FILE="${SOPR_ENV_FILE:-$env_file}"
  export PREPMASTER_ENV_FILE="${PREPMASTER_ENV_FILE:-$env_file}"
}
