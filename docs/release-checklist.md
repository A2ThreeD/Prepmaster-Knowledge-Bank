# Release Checklist

Use this checklist when validating SOPR on a fresh Raspberry Pi or preparing a new release.

## Source Control

1. Push the latest commits from the main development machine.
2. Confirm the repository is clean before release:
   ```bash
   git status --short
   ```

## Fresh Pi Setup

1. Clone the repository on the Pi:
   ```bash
   git clone <repo-url> sopr
   cd sopr
   ```
2. Create the local runtime config:
   ```bash
   cp config/prepmaster.env.example config/prepmaster.env
   ```
3. Set the important values in `config/prepmaster.env`:
   - `PREPMASTER_HOSTNAME`
   - `PREPMASTER_WIKIPEDIA_OPTION`
   - `PREPMASTER_ZIM_MODE`
   - `PREPMASTER_AP_ENABLED`
   - AP SSID/passphrase if AP mode will be tested

## Base Install

1. Run the installer:
   ```bash
   sudo ./scripts/install_sopr.sh
   ```
2. Confirm core services are healthy:
   ```bash
   systemctl status prepmaster-portal.service --no-pager
   systemctl status prepmaster-kiwix.service --no-pager
   systemctl status nginx --no-pager
   ```

## Browser Validation

1. Open the main site:
   - `http://<pi-ip>/`
   - or `http://sopr.local/` if mDNS resolves
2. Complete setup in the browser.
3. Click `Apply Configuration`.
4. Confirm the dashboard becomes the default homepage after setup.

## Route Checks

Verify these locations:

- `/`
- `/admin/`
- `/maps/`
- `/kiwix/` redirects to the dedicated Kiwix port

## Kiwix Checks

1. Confirm Kiwix opens correctly on the redirected port.
2. Confirm downloaded ZIM files exist:
   ```bash
   ls -lh /library/zims/content
   ```
3. Confirm the Kiwix service is using the expected bind address and port:
   ```bash
   systemctl status prepmaster-kiwix.service --no-pager
   ```

## Status And API Checks

1. Confirm the dashboard/admin status areas show:
   - disk usage
   - temperature
   - uptime
   - service health
   - Kiwix target URL
2. Confirm the API endpoints respond:
   ```bash
   curl http://127.0.0.1/api/state
   curl http://127.0.0.1/api/status
   curl http://127.0.0.1/api/apply
   ```

## AP Mode Checks

If AP mode is disabled:

```bash
systemctl status hostapd --no-pager
systemctl status dnsmasq --no-pager
```

Expected result:
- `hostapd` inactive
- `dnsmasq` inactive

If AP mode is enabled:

1. Confirm the AP SSID is visible.
2. Join the Pi directly from another device.
3. Confirm the site and Kiwix are reachable over the AP network.

## Release Notes To Capture

Record these items for each release validation:

- Raspberry Pi model
- OS version
- storage size
- whether AP mode was tested
- whether quick-test or full content mode was used
- any manual recovery steps needed during validation

## Git Hygiene

Do not commit Pi-local runtime files:

- `config/prepmaster.env`
- `config/install-profile.env`
- `config/kiwix-zim-urls.txt`

Before finishing:

```bash
git status --short
```

The working tree should be clean or only contain intentionally local runtime changes.
