# SOPR Architecture Notes

## Goals

- Run on a fresh Raspberry Pi OS Lite install
- Expose an emergency-focused landing page
- Provide quick access to offline knowledge, maps, and system tools
- Keep the install reproducible with shell automation

## Planned Major Components

### 1. Base Operating System

- Raspberry Pi OS Lite
- Regular package updates through `apt`
- Avahi for local hostname discovery
- Optional wireless access point mode for direct client connectivity

### 2. Offline Knowledge

- Kiwix content served from `/library/zims/content`
- `kiwix-serve` as the content server
- Curated ZIM list generated from `catalog/kiwix-categories.yaml`
- Install profiles that scale from essential to comprehensive
- A user-selectable Wikipedia package sourced from `catalog/wikipedia.yaml`

### 3. Offline Maps

The project will use a custom maps path rather than IIAB. That keeps the deployment smaller and easier to reason about on Raspberry Pi OS Lite.

Likely implementation options:

- Static map assets with a lightweight browser viewer
- A local tile or vector-map service if we decide regional coverage is worth the storage cost

### 4. Web Experience

- Main page headline and branding should reflect `SOPR` and expand to `Survival Operations Plan Response` where helpful
- First startup should present a basic configuration page before the normal dashboard experience
- First-start choices should map directly to install-profile flags consumed by the shell installer
- Once setup is marked complete, `/` should show the dashboard instead of the setup flow
- Core categories: Quick Access, Critical, Sustainment, Rebuild, System Tools
- Top-level quick links: `/kiwix`, `/maps`, `/admin`
- A separate system status section at the bottom for disk usage, temperature, and future battery state
- The settings/admin area should include AP configuration and enablement controls
- The UI should favor plain language for non-technical users and avoid internal terms where a simpler label will work
- Each screen should present one clear decision at a time with one obvious next action
- Technical detail belongs in secondary views, status panels, or documentation rather than the main setup flow
- User-facing SOPR screens should keep SOPR branding primary. Upstream projects, catalogs, and source ecosystems can be credited in documentation or quiet attribution notes, but section titles and main interaction labels should remain SOPR-specific and generic for end users

### 5. Local Portal API

- A lightweight local API persists setup state and exposes live Raspberry Pi status
- The homepage and admin pages use that API to reopen setup preferences and display system metrics
- The same API now exposes a background apply workflow so the web UI can trigger backend scripts directly

### 6. Service Routing

- Nginx serves the main site and static sections
- `/kiwix` redirects the browser to the dedicated Kiwix server port
- `/api` proxies to the local portal API
- `/admin` and `/maps` are served as static app sections
- The dedicated Kiwix port serves either the real Kiwix library or a placeholder page if no library is available yet

## Suggested Install Phases

### Phase 1

- Prepare Raspberry Pi OS
- Install common packages
- Create directories
- Install project placeholders
- Download ZIM content

### Phase 2

- Configure `kiwix-serve` as a systemd service
- Stand up the landing page and reverse proxy
- Build the custom `/admin` and `/maps` experiences

### Phase 3

- Theme admin pages to match the landing page
- Add live system status widgets
- Add maintenance/update scripts

## Assumptions In This Initial Scaffold

- Target device is a Raspberry Pi 4 or Pi 5 with enough storage for large ZIM and map assets
- Install is performed as `root` or with `sudo`
- Internet is available during initial provisioning

## Upstream References

These informed the scaffold and should guide the next integration steps:

- Kiwix `kiwix-serve` documentation
- Raspberry Pi OS package-management guidance
