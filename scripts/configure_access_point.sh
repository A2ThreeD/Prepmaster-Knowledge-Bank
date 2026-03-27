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

if [[ "${PREPMASTER_AP_ENABLED:-0}" != "1" ]]; then
  echo "Access point mode is disabled in config/prepmaster.env."
  echo "Stopping and disabling AP-related services."
  systemctl stop hostapd 2>/dev/null || true
  systemctl stop dnsmasq 2>/dev/null || true
  systemctl stop prepmaster-ap-network.service 2>/dev/null || true
  systemctl disable hostapd 2>/dev/null || true
  systemctl disable dnsmasq 2>/dev/null || true
  systemctl disable prepmaster-ap-network.service 2>/dev/null || true
  exit 0
fi

if [[ ${#PREPMASTER_AP_PASSPHRASE} -lt 8 || ${#PREPMASTER_AP_PASSPHRASE} -gt 63 ]]; then
  echo "PREPMASTER_AP_PASSPHRASE must be between 8 and 63 characters."
  exit 1
fi

echo "Installing access point packages..."
apt install -y hostapd dnsmasq

echo "Stopping services before reconfiguration..."
systemctl stop hostapd || true
systemctl stop dnsmasq || true

if command -v rfkill >/dev/null 2>&1; then
  echo "Unblocking Wi-Fi radio..."
  rfkill unblock wifi || true
fi

install -d -m 0755 /etc/hostapd
install -d -m 0755 /etc/dnsmasq.d

cat > /etc/hostapd/hostapd.conf <<EOF
country_code=$PREPMASTER_AP_COUNTRY
interface=$PREPMASTER_AP_INTERFACE
driver=nl80211
ssid=$PREPMASTER_AP_SSID
hw_mode=g
channel=$PREPMASTER_AP_CHANNEL
ieee80211n=1
wmm_enabled=1
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=$PREPMASTER_AP_PASSPHRASE
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF

cat > /etc/default/hostapd <<EOF
DAEMON_CONF="/etc/hostapd/hostapd.conf"
EOF

cat > /etc/dnsmasq.d/prepmaster-ap.conf <<EOF
interface=$PREPMASTER_AP_INTERFACE
bind-interfaces
domain-needed
bogus-priv
dhcp-range=$PREPMASTER_AP_DHCP_START,$PREPMASTER_AP_DHCP_END,$PREPMASTER_AP_NETMASK,$PREPMASTER_AP_DHCP_LEASE
dhcp-option=3,$PREPMASTER_AP_ADDRESS
dhcp-option=6,$PREPMASTER_AP_ADDRESS
address=/prepmaster.local/$PREPMASTER_AP_ADDRESS
EOF

cat > /etc/systemd/system/prepmaster-ap-network.service <<EOF
[Unit]
Description=Configure Prepmaster AP network interface
Before=hostapd.service dnsmasq.service
Wants=network-pre.target
After=network-pre.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/ip link set $PREPMASTER_AP_INTERFACE down
ExecStart=/usr/sbin/ip addr flush dev $PREPMASTER_AP_INTERFACE
ExecStart=/usr/sbin/ip addr add $PREPMASTER_AP_ADDRESS/$PREPMASTER_AP_CIDR dev $PREPMASTER_AP_INTERFACE
ExecStart=/usr/sbin/ip link set $PREPMASTER_AP_INTERFACE up
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

echo "Reloading systemd and enabling AP services..."
systemctl daemon-reload
systemctl unmask hostapd || true
systemctl enable prepmaster-ap-network.service
systemctl enable hostapd
systemctl enable dnsmasq
systemctl restart prepmaster-ap-network.service
systemctl restart hostapd
systemctl restart dnsmasq

echo "Access point configuration complete."
echo "SSID: $PREPMASTER_AP_SSID"
echo "Gateway: $PREPMASTER_AP_ADDRESS"
