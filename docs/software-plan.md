# SOPR Software Plan

This document describes the software stack SOPR uses today on a Raspberry Pi, what the browser-driven setup flow now controls, and which work areas still need attention.

## Current Product Shape

SOPR is now an offline-first appliance-style Raspberry Pi stack with:

- a first-start setup flow on `/`
- a dashboard view after setup is complete
- a settings/admin interface on `/admin`
- an offline maps viewer on `/maps`
- Kiwix served behind the SOPR site
- an optional wireless access point mode

The project is no longer just a scaffold. The software plan should now be read as `current software baseline + next software priorities`.

## Core Runtime Stack

### Base System Packages

Installed by `scripts/bootstrap_pi.sh` and used by the current stack:

- `avahi-daemon` for LAN hostname discovery
- `ca-certificates` for HTTPS downloads
- `curl` and `wget` for scripted downloads
- `git` and `git-lfs` for upstream source retrieval and large map assets
- `hostapd` for wireless access point mode
- `dnsmasq` for DHCP/DNS on the local access point
- `jq` for JSON-friendly shell workflows
- `kiwix-tools` for `kiwix-serve` and `kiwix-manage`
- `nginx` for the main site and reverse proxy layer
- `python3`, `python3-pip`, and `python3-venv` for portal and helper tooling
- `rsync` for file sync operations
- `sqlite3` for lightweight local state if needed later
- `ufw` for firewall management
- `unzip` and `xz-utils` for downloaded assets

### Runtime Services

The stack currently expects these service roles:

- `prepmaster-portal.service` for setup state, status APIs, and background workflows
- `prepmaster-kiwix.service` for `kiwix-serve`
- `nginx` for `/`, `/admin`, `/maps`, `/api`, and Kiwix proxying
- `hostapd` when wireless AP mode is enabled
- `dnsmasq` when wireless AP mode is enabled
- `prepmaster-ap-network.service` when the device hotspot is enabled

## Current User-Facing Software Features

### 1. First-Start Setup

The setup flow on `/` now supports:

- module selection
- Wikipedia package selection
- offline map region selection
- network mode selection
- review + complete setup
- live apply status and refreshing logs
- storage estimation with warnings and hard blocking when space is insufficient
- resume behavior after reboot if setup work was interrupted

The setup flow is intentionally plain-language and optimized for non-technical users.

### 2. Offline Knowledge

SOPR currently supports:

- curated ZIM selection from `catalog/kiwix-categories.yaml`
- selectable Wikipedia packages from `catalog/wikipedia.yaml`
- generated manifests via `scripts/build_kiwix_zim_manifest.py`
- optional `quick-test` mode for smaller validation downloads
- installed-content management in `/admin`
- online catalog browsing for more ZIM downloads
- disk-usage preview for additional ZIM selections

### 3. Offline Maps

SOPR now supports:

- local PMTiles storage under `/maps`
- MapLibre-based browser viewing on `/maps`
- active-map centering from PMTiles bounds when possible
- region-based offline map selection during setup
- region-based map management in `/admin`
- local cached regional map catalog data in `catalog/nomad-maps.json`
- size-aware regional map selection with disk warnings
- background map sync with progress and log output

### 4. Wireless Access Point

The admin system page currently supports:

- viewing AP status
- editing AP configuration
- enabling and disabling the AP
- applying AP settings without leaving the UI
- viewing connected clients

### 5. Kiwix Integration

The current Kiwix behavior includes:

- Kiwix running on its own service/port
- Nginx proxy handling so the site can route users cleanly into Kiwix
- library rebuild support after adding or removing ZIMs
- placeholder behavior when no library exists yet

## Current Configuration Sources

### Main Editable Runtime Config

- `config/prepmaster.env`
- `config/prepmaster.env.example`

This covers:

- hostname
- content mode
- Wikipedia choice
- map storage/config paths
- map catalog source
- wireless AP settings
- Kiwix-related paths and ports

### Install Profile

- `config/install-profile.env`
- `config/install-profile.env.example`

This currently tracks install-profile style choices such as optional education add-ons.

### Catalog Sources

- `catalog/kiwix-categories.yaml`
- `catalog/wikipedia.yaml`
- `catalog/nomad-maps.json`

These now act as SOPR’s local source-of-truth catalogs for:

- curated knowledge/content groupings
- Wikipedia package choices
- regional offline map choices

## Current Script Responsibilities

### Installation / System

- `scripts/bootstrap_pi.sh`
- `scripts/install_sopr.sh`
- `scripts/install_portal_service.sh`
- `scripts/install_kiwix_service.sh`
- `scripts/install_nginx_site.sh`
- `scripts/install_maps_assets.sh`

### Content / Catalog

- `scripts/build_kiwix_zim_manifest.py`
- `scripts/build_wikipedia_options.py`
- `scripts/download_kiwix_zims.sh`
- `scripts/rebuild_kiwix_library.sh`

### Optional Components

- `scripts/install_optional_components.sh`
- `scripts/install_kolibri.sh`
- `scripts/install_ka_lite_legacy.sh`

### Wireless Networking

- `scripts/configure_access_point.sh`

## Current Design/UX Software Rules

These conventions are now part of the product and should be treated as implementation rules, not just preferences:

- plain language for non-technical users
- one main decision per screen or card where possible
- beveled pressable controls should look obviously interactive
- status boxes should read differently from buttons
- white scrollbars on dark scrollable panels
- SOPR branding stays primary in the user-facing UI
- upstream sources should be credited quietly, not used as primary feature branding

## Work That Is Implemented Now

The following items are no longer “future plan” items:

- browser-driven setup and apply flow
- setup-complete state with dashboard switching
- live apply progress/status API
- AP control UI in settings
- connected-clients view for the AP
- offline maps viewer using PMTiles
- region-based map selection and sync
- installed-content management in settings
- Kiwix proxy routing through Nginx

## Near-Term Software Priorities

These are the main areas still worth iterating on next:

- external storage usage capabilities and management
- better map metadata and descriptions where upstream data is sparse
- stronger documentation for local catalog refresh/update workflows
- possible backup/export workflows for saved setup state and content choices
- more explicit update/maintenance flows for already-installed devices
- better test/validation tooling for fresh Pi installs and browser flows
- optional health/status improvements around disk, temperature, and long-running sync jobs

## Longer-Term Software Ideas

- media hosting (plex/jellyfin)
- NAS capabilities
- optional battery/power awareness in the dashboard
- richer offline search for maps
- better local diagnostics and recovery tools in `/admin`
- a more formal update channel for catalogs and application assets
- additional packaged offline services beyond Kiwix and maps when they fit SOPR’s storage and reliability goals

## Operational Guidance

For release validation and fresh Pi testing, use:

- [release-checklist.md](/home/prepper/prepmaster/docs/release-checklist.md)

For structural and UX principles, use:

- [architecture.md](/home/prepper/prepmaster/docs/architecture.md)
