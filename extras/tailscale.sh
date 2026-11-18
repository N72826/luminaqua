#!/bin/bash
# Install Tailscale from its official Debian package source and sign this Pi in.
set -euo pipefail
. /etc/os-release   # VERSION_CODENAME, e.g. trixie

echo "== Tailscale package source ($VERSION_CODENAME)"
curl -fsSL "https://pkgs.tailscale.com/stable/debian/$VERSION_CODENAME.noarmor.gpg" -o /tmp/ts.gpg
curl -fsSL "https://pkgs.tailscale.com/stable/debian/$VERSION_CODENAME.tailscale-keyring.list" -o /tmp/ts.list
cat /tmp/ts.list
sudo install -m 0644 /tmp/ts.gpg /usr/share/keyrings/tailscale-archive-keyring.gpg
sudo install -m 0644 /tmp/ts.list /etc/apt/sources.list.d/tailscale.list
rm /tmp/ts.gpg /tmp/ts.list

echo "== Installing"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y tailscale
sudo systemctl enable --now tailscaled
systemctl is-active tailscaled

echo "== Sign in: open the link below and approve this Pi"
sudo tailscale up --hostname=luminaqua
tailscale ip -4
echo TAILSCALE-DONE
