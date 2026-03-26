# Survival Operations Plan Response

SOPR is an offline-first Raspberry Pi project for serving emergency preparedness, medical, survival, and general reference material on a local network.

The current plan is to build on a fresh Raspberry Pi OS Lite install and automate:

- Base OS preparation
- Required package installation
- Directory layout for content and site assets
- Custom web, admin, and offline PMTiles maps scaffold
- Kiwix ZIM downloads into `/library/zims/content`
- ZIM selection generated from Project NOMAD's `kiwix-categories.json`

## Current Repository Layout

- `instructions.md` - original project requirements
- `scripts/bootstrap_pi.sh` - prepares a clean Raspberry Pi OS Lite system
- `scripts/install_prepmaster.sh` - top-level installer for the project
- `scripts/build_kiwix_zim_manifest.py` - generates the ZIM manifest from Project NOMAD categories
- `scripts/download_kiwix_zims.sh` - curated Kiwix ZIM downloader
- `scripts/select_install_profile.sh` - records first-start install choices for optional education add-ons
- `scripts/install_optional_components.sh` - installs optional education components from the selected profile
- `scripts/install_kolibri.sh` - installs Kolibri from Learning Equality's package source
- `scripts/install_ka_lite_legacy.sh` - guarded legacy path for KA Lite requests
- `scripts/configure_access_point.sh` - configures the Pi as a wireless access point for direct client access
- `scripts/install_portal_service.sh` - installs the local portal API service for setup state and live status
- `scripts/install_kiwix_service.sh` - installs and enables the local `kiwix-serve` systemd service
- `scripts/rebuild_kiwix_library.sh` - rebuilds the Kiwix `library.xml` from downloaded ZIM files
- `scripts/run_kiwix_service.sh` - runtime wrapper that starts Kiwix or a placeholder page when the library is empty
- `scripts/install_nginx_site.sh` - installs the Nginx site that serves `/`, `/kiwix`, `/maps`, `/admin`, and `/api`
- `app/prepmaster_portal.py` - lightweight API for persisted setup state and Raspberry Pi status
- `config/prepmaster.env.example` - template for editable install settings
- `config/install-profile.env.example` - template for first-start install profile defaults
- `config/kiwix-zim-urls.quick-test.txt` - tiny starter manifest for fast Kiwix validation
- `wikipedia.json` - selectable Wikipedia ZIM options for the configuration flow
- `docs/architecture.md` - stack and design notes
- `docs/release-checklist.md` - step-by-step validation list for fresh Pi installs and release testing
- `docs/software-plan.md` - software checklist for the Pi
- `index.html.framework` - backup design-language reference for the UI
- `web/admin/index.html` - starter admin page placeholder
- `web/maps/index.html` - offline PMTiles maps viewer powered by MapLibre

## Intended Software Stack

- Raspberry Pi OS Lite
- Kiwix / `kiwix-serve`
- Nginx as the outer landing page / reverse proxy layer
- Avahi for local `.local` discovery on LAN
- Optional wireless AP mode for direct client access
- Custom admin and PMTiles-based maps components built in-repo

This is an initial scaffold for a fully custom stack. It is designed to give us a repeatable starting point on a fresh Pi while keeping the web layer, admin flow, and future maps integration under our control.

## Fresh Pi Workflow

On the Raspberry Pi:

```bash
git clone <this-repo-url> prepmaster
cd prepmaster
cp config/prepmaster.env.example config/prepmaster.env
sudo ./scripts/install_prepmaster.sh
```

If you want to only prepare the OS first:

```bash
sudo ./scripts/bootstrap_pi.sh
```

To fetch the current starter content set:

```bash
sudo ./scripts/download_kiwix_zims.sh
```

## Notes

- The installer now assumes a custom stack with no IIAB dependency.
- `kiwix-categories.json` from Project NOMAD is now the source of truth for curated ZIM selection.
- The generated manifest in `config/kiwix-zim-urls.txt` is built from that JSON using `PREPMASTER_ZIM_PROFILE`.
- The selected Wikipedia variant is controlled by `PREPMASTER_WIKIPEDIA_OPTION` and sourced from `wikipedia.json`.
- Supported profiles are `essential`, `standard`, and `comprehensive`.
- Set `PREPMASTER_ZIM_MODE=quick-test` if you want a small validation download before pulling the full content set.
- The main page is now intended to be a first-start configuration screen with base install selected by default.
- The main page now switches automatically between first-start setup mode and the normal dashboard based on saved setup state.
- The setup page can now save preferences and trigger the backend apply workflow directly from the browser.
- Kiwix now runs on a dedicated port, and the dashboard/admin views expose the real Kiwix target URL.
- If no Kiwix library exists yet, the dedicated Kiwix port serves a placeholder page instead of crash-looping.
- `/maps` now expects a local PMTiles archive under the configured PMTiles root and renders it through MapLibre instead of a raster `z/x/y.png` tile tree.
- Optional education add-ons are tracked separately: `Kolibri` as a modern add-on, and `KA Lite` as a legacy add-on.
- `Kolibri` now has install automation in the repo. `KA Lite` requires an explicit legacy override and currently records the request rather than forcing a risky unattended install on modern Raspberry Pi OS.
- Wireless AP mode can be enabled through `config/prepmaster.env` and applied by the installer or `scripts/configure_access_point.sh`.
- The installer now provisions `kiwix-serve`, the portal API, and an Nginx site so `/`, `/maps`, `/admin`, and `/api` are served on the main site, while `/kiwix` redirects the browser to the dedicated Kiwix port.

## Validation

Use [docs/release-checklist.md](/Volumes/External/Projects/Prepmaster%20Knowledge%20Bank/docs/release-checklist.md) when validating a fresh Pi install or preparing a new release.
