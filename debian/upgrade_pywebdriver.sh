#!/bin/bash
# Upgrade an installed pywebdriver to a trobz/pywebdriver release, keeping
# /etc/pywebdriver/config.ini as it is. For a first installation, use install.sh.
# Launch without sudo: the script calls sudo where needed.
#
# Usage: ./upgrade_pywebdriver.sh [--version vX.Y.Z] [--no-ssl]
#   --version vX.Y.Z  install this release instead of the latest one
#                     (also the way to roll back to an older release)
#   --no-ssl          serve over plain HTTP: comment out sslcert/sslkey in
#                     config.ini, like answering "n" to install.sh

set -euo pipefail

REPO="trobz/pywebdriver"
CONFIG=/etc/pywebdriver/config.ini
VERSION="latest"
NO_SSL=0

while [ $# -gt 0 ]; do
	case "$1" in
	--version)
		VERSION="${2:?--version needs a release tag, e.g. v3.0.50}"
		shift 2
		;;
	--no-ssl)
		NO_SSL=1
		shift
		;;
	-h | --help)
		sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	*)
		echo "Unknown option: $1 (see --help)" >&2
		exit 2
		;;
	esac
done

info() { echo -e "\e[1;33m## $* \e[0m"; }
ok() { echo -e "\e[1;32m## $* \e[0m"; }
fail() {
	echo -e "\e[1;31m## $* \e[0m" >&2
	exit 1
}

if ! dpkg-query -W -f='${Status}' pywebdriver 2>/dev/null | grep -q "install ok installed"; then
	fail "pywebdriver is not installed: use install.sh for a first installation"
fi
[ -f "$CONFIG" ] || fail "$CONFIG not found"

CODENAME=$(. /etc/os-release && echo "${UBUNTU_CODENAME:-}")
[ -n "$CODENAME" ] || fail "Can't find the Ubuntu codename in /etc/os-release"
if [ "$VERSION" = "latest" ]; then
	URL="https://github.com/$REPO/releases/latest/download/pywebdriver_${CODENAME}.deb"
else
	URL="https://github.com/$REPO/releases/download/${VERSION}/pywebdriver_${CODENAME}.deb"
fi

BEFORE=$(dpkg-query -W -f='${Version}' pywebdriver)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
DEB="$TMP/pywebdriver_${CODENAME}.deb"

info "Downloading $URL"
curl -fsSL -o "$DEB" "$URL" || fail "Download failed: no ${CODENAME} package for release '$VERSION'?"
[ "$(dpkg-deb -f "$DEB" Package)" = "pywebdriver" ] || fail "The downloaded file is not a pywebdriver package"
TARGET=$(dpkg-deb -f "$DEB" Version)
info "Installed build: $BEFORE, downloaded build: $TARGET"

BACKUP="$CONFIG.bak-$(date +%Y%m%d-%H%M%S)"
sudo cp -a "$CONFIG" "$BACKUP"
info "Config backed up to $BACKUP"

# --force-confold keeps the current config.ini instead of the package's default
# one (port, host, drivers...). No "apt update": the package's dependencies are
# already installed, and it can fail on old Ubuntu releases.
info "Installing the package"
if ! sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --reinstall \
	-o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef "$DEB"; then
	fail "apt-get install failed. If it reports missing dependencies, run 'sudo apt update', then this script again."
fi

if [ "$NO_SSL" = 1 ]; then
	info "Disabling SSL"
	sudo sed -i 's/^ *sslkey.*/;sslkey/; s/^ *sslcert.*/;sslcert/' "$CONFIG"
fi

info "Restarting the pywebdriver service"
sudo systemctl restart pywebdriver
sleep 5

AFTER=$(dpkg-query -W -f='${Version}' pywebdriver)
STATE=$(systemctl is-active pywebdriver || true)
HOST=$(sed -n '/^\[flask\]/,/^\[/p' "$CONFIG" | sed -n 's/^ *host *= *//p' | head -1)
PORT=$(sed -n '/^\[flask\]/,/^\[/p' "$CONFIG" | sed -n 's/^ *port *= *//p' | head -1)
if grep -qE '^ *sslcert *=' "$CONFIG"; then SCHEME=https; else SCHEME=http; fi
HELLO=$(curl -sk -m 5 "$SCHEME://${HOST:-127.0.0.1}:${PORT:-8069}/hw_proxy/hello" || true)

echo "  build:   $BEFORE -> $AFTER"
echo "  service: $STATE"
if sudo cmp -s "$BACKUP" "$CONFIG"; then
	echo "  config:  unchanged"
else
	echo "  config:  changed:"
	sudo diff "$BACKUP" "$CONFIG" | sed 's/^/    /' || true
fi
echo "  check:   $SCHEME://${HOST:-127.0.0.1}:${PORT:-8069}/hw_proxy/hello -> ${HELLO:-no answer}"

if [ "$STATE" != "active" ] || [ "$HELLO" != "ping" ]; then
	fail "pywebdriver is not answering. Check 'journalctl -feu pywebdriver'. To restore the previous config: sudo cp -a $BACKUP $CONFIG && sudo systemctl restart pywebdriver"
fi
ok "pywebdriver upgraded and answering"
