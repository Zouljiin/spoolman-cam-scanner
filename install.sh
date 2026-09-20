#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Spoolman Cam Scanner installer - Copyright (C) 2026 Zouljiin
# https://github.com/Zouljiin/spoolman-cam-scanner
#
#   curl -fsSL https://raw.githubusercontent.com/Zouljiin/spoolman-cam-scanner/main/install.sh | sudo bash
#   sudo bash install.sh [install|update|uninstall] [options]      (see --help)
#
# Debian / Ubuntu / Raspberry Pi OS / Proxmox containers. Needs root (it installs to /opt and a
# systemd service). Nothing is installed on your printers.
set -eu

REPO="Zouljiin/spoolman-cam-scanner"
REPO_URL="${SCANNER_REPO_URL:-https://github.com/$REPO.git}"
TARBALL_BASE="${SCANNER_TARBALL_BASE:-https://github.com/$REPO/archive}"
SERVICE="spool-cam-scanner"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
DIR="/opt/spoolman-cam-scanner"
REF="main"
SVC_USER="spoolscan"
CMD="install"
NO_SERVICE=0
ASSUME_YES=0
PURGE=0
FROM_DIR=""

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
cat <<USAGE
Spoolman Cam Scanner installer

Usage: sudo bash install.sh [install|update|uninstall] [options]

  install      (default) install to $DIR, set up a Python virtualenv, create config.json,
               and optionally start it as a systemd service
  update       fetch the latest version, refresh dependencies, restart the service
  uninstall    stop and remove the service and program files (config.json is kept unless --purge)

Options:
  --dir PATH      install location (default: $DIR)
  --ref NAME      branch or tag to install (default: $REF), for example v0.1.0
  --user NAME     system user the service runs as (default: $SVC_USER)
  --no-service    don't create or start a systemd service
  --yes           never ask questions (leaves the example config in place and does not start the service)
  --purge         with uninstall: also delete config.json and the service user
  --from-dir PATH install from a local copy of the project instead of downloading
  -h, --help      show this help
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        install|update|uninstall) CMD="$1" ;;
        --dir)        [ $# -ge 2 ] || die "--dir needs a value"; DIR="$2"; shift ;;
        --ref)        [ $# -ge 2 ] || die "--ref needs a value"; REF="$2"; shift ;;
        --user)       [ $# -ge 2 ] || die "--user needs a value"; SVC_USER="$2"; shift ;;
        --from-dir)   [ $# -ge 2 ] || die "--from-dir needs a value"; FROM_DIR="$2"; shift ;;
        --no-service) NO_SERVICE=1 ;;
        --yes|-y)     ASSUME_YES=1 ;;
        --purge)      PURGE=1 ;;
        -h|--help)    usage; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

case "$DIR" in
    /|"") die "refusing to use '$DIR' as the install directory" ;;
    /*) ;;
    *) die "--dir must be an absolute path" ;;
esac
DIR="${DIR%/}"
[ "$(id -u)" -eq 0 ] || die "please run as root, for example: curl -fsSL <url> | sudo bash"

# ---- helpers ------------------------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }
interactive() { [ "$ASSUME_YES" -eq 0 ] && [ -r /dev/tty ] && [ -w /dev/tty ] && (: </dev/tty) 2>/dev/null; }
ask() {  # ask "question" "default" -> answer on stdout
    if interactive; then
        printf '%s [%s]: ' "$1" "$2" >/dev/tty
        read -r ans </dev/tty || ans=""
        printf '%s' "${ans:-$2}"
    else
        printf '%s' "$2"
    fi
}
confirm() {  # confirm "question" default(y|n)
    [ "$ASSUME_YES" -eq 0 ] || return 0
    interactive || { [ "$2" = y ]; return; }
    local hint="Y/n"; [ "$2" = n ] && hint="y/N"
    printf '%s [%s]: ' "$1" "$hint" >/dev/tty
    read -r ans </dev/tty || ans=""
    ans="${ans:-$2}"
    case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}
have_systemd() { { [ -d /run/systemd/system ] || [ -n "${SCANNER_ASSUME_SYSTEMD:-}" ]; } && have systemctl; }   # SCANNER_ASSUME_SYSTEMD is for tests
service_exists() { [ -f "$SYSTEMD_DIR/$SERVICE.service" ]; }
apt_install() { have apt-get || return 1; DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"; }

# ---- steps --------------------------------------------------------------------------------
check_python() {
    have python3 || { say "python3 not found, trying to install it"; apt_install python3 python3-venv python3-pip || die "python3 is required"; }
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "Python 3.9 or newer is required (found $(python3 -V 2>&1))"
}

get_source() {
    mkdir -p "$DIR"
    if [ -n "$FROM_DIR" ]; then
        [ -f "$FROM_DIR/spool_cam_scanner.py" ] || die "$FROM_DIR does not look like the project (no spool_cam_scanner.py)"
        say "Copying from $FROM_DIR"
        tar -C "$FROM_DIR" --exclude=./venv --exclude=./config.json --exclude=./.git -cf - . | tar -C "$DIR" -xf -
    elif have git; then
        if [ -d "$DIR/.git" ]; then
            say "Updating $DIR ($REF)"
            git -c safe.directory="$DIR" -C "$DIR" fetch --quiet --depth 1 origin "$REF"
            git -c safe.directory="$DIR" -C "$DIR" reset --quiet --hard FETCH_HEAD
        else
            # a leftover config.json from an earlier uninstall is fine; anything else is not ours to overwrite
            [ -z "$(find "$DIR" -mindepth 1 -maxdepth 1 -not -name 'config.json*' 2>/dev/null | head -1)" ] \
                || die "$DIR already exists and is not empty (and isn't a git checkout). Use --dir for another location."
            say "Downloading $REPO ($REF)"
            local tmp; tmp="$(mktemp -d "$DIR.clone.XXXXXX")"
            git clone --quiet --depth 1 --branch "$REF" "$REPO_URL" "$tmp/src" || { rm -rf "$tmp"; die "git clone failed"; }
            cp -a "$tmp/src/." "$DIR/"
            rm -rf "$tmp"
        fi
    elif have curl; then
        say "Downloading $REPO ($REF) with curl"
        curl -fsSL "$TARBALL_BASE/$REF.tar.gz" | tar -xz -C "$DIR" --strip-components=1
    else
        die "need git or curl to download the project"
    fi
    [ -f "$DIR/spool_cam_scanner.py" ] || die "download failed: $DIR/spool_cam_scanner.py is missing"
}

setup_venv() {
    if [ ! -x "$DIR/venv/bin/python" ]; then
        say "Creating the Python virtual environment"
        python3 -m venv "$DIR/venv" 2>/dev/null || {
            warn "python3 -m venv failed; installing python3-venv"
            apt_install python3-venv python3-pip || die "could not create a virtual environment (install python3-venv)"
            python3 -m venv "$DIR/venv"
        }
    fi
    say "Installing Python packages (opencv, numpy, requests) - this can take a minute"
    "$DIR/venv/bin/pip" install --quiet -r "$DIR/requirements.txt"
}

ensure_user() {
    id -u "$SVC_USER" >/dev/null 2>&1 && return 0
    say "Creating system user '$SVC_USER'"
    useradd --system --home-dir "$DIR" --no-create-home --shell "$(command -v nologin || echo /usr/sbin/nologin)" "$SVC_USER"
}

write_config() {
    CONFIG_IS_REAL=0
    [ -f "$DIR/config.json" ] && { CONFIG_IS_REAL=1; return 0; }
    if interactive; then
        say "Let's set up your first printer (you can add more later)"
        local name addr
        name="$(ask 'Printer name' 'Printer 1')"
        addr="$(ask "Moonraker address of '$name' (e.g. 192.168.1.50 or http://192.168.1.50:7125)" '')"
        if [ -n "$addr" ]; then
            python3 - "$name" "$addr" "$DIR/config.json" <<'PY'
import json, sys
name, addr, path = sys.argv[1:4]
if "://" not in addr:
    addr = "http://" + addr + ":7125"
cfg = {"storage_location": "Shelf",
       "web": {"enabled": True, "port": 8090},
       "printers": [{"name": name, "moonraker": addr.rstrip("/")}]}
json.dump(cfg, open(path, "w"), indent=2)
open(path, "a").write("\n")
PY
            CONFIG_IS_REAL=1
            return 0
        fi
    fi
    cp "$DIR/config.example.json" "$DIR/config.json"
    warn "created $DIR/config.json from the example. Edit it and put in your printers before starting the service."
}

install_service() {
    [ "$NO_SERVICE" -eq 0 ] || return 0
    have_systemd || { warn "systemd not found: skipping the service. Run it by hand: $DIR/venv/bin/python $DIR/spool_cam_scanner.py -c $DIR/config.json"; return 0; }
    cat >"$SYSTEMD_DIR/$SERVICE.service" <<UNIT
[Unit]
Description=Spoolman camera QR scanner (printer webcams -> active spool)
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python spool_cam_scanner.py -c config.json
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    if [ "$CONFIG_IS_REAL" -eq 1 ] && confirm "Start the scanner now and on every boot?" y; then
        systemctl enable --now "$SERVICE"
        sleep 3
        if systemctl is-active --quiet "$SERVICE"; then say "The scanner service is running."; else warn "the service did not start; see: journalctl -u $SERVICE -n 30"; fi
    else
        say "Service installed but not started. When your config.json is ready:  sudo systemctl enable --now $SERVICE"
    fi
}

finish_message() {
    local ip; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo
    say "Done. $("$DIR/venv/bin/python" "$DIR/spool_cam_scanner.py" --version)"
    cat <<MSG

  Config:       $DIR/config.json      (edit, then: sudo systemctl restart $SERVICE)
  Test safely:  sudo -u $SVC_USER $DIR/venv/bin/python $DIR/spool_cam_scanner.py -c $DIR/config.json --dry-run
  Logs:         journalctl -u $SERVICE -f
  Live page:    http://${ip:-<this-machine>}:8090/     (needs "web": {"enabled": true} in config.json)
  Update:       sudo bash $DIR/install.sh update
  Docs:         https://github.com/$REPO#documentation

MSG
}

# ---- commands -----------------------------------------------------------------------------
do_install() {
    check_python
    get_source
    setup_venv
    ensure_user
    write_config
    chown -R "$SVC_USER:$SVC_USER" "$DIR"
    chmod 600 "$DIR/config.json"
    install_service
    finish_message
}

do_update() {
    [ -f "$DIR/spool_cam_scanner.py" ] || die "no installation found in $DIR (use install, or --dir)"
    check_python
    get_source
    setup_venv
    id -u "$SVC_USER" >/dev/null 2>&1 && chown -R "$SVC_USER:$SVC_USER" "$DIR"
    if have_systemd && service_exists; then
        systemctl try-restart "$SERVICE" && say "Restarted $SERVICE"
    fi
    say "Updated. $("$DIR/venv/bin/python" "$DIR/spool_cam_scanner.py" --version)"
}

do_uninstall() {
    [ -f "$DIR/spool_cam_scanner.py" ] || die "no installation found in $DIR"
    if [ "$PURGE" -eq 1 ]; then
        confirm "Delete $DIR INCLUDING your config.json, and remove user '$SVC_USER'?" n || die "cancelled"
    else
        confirm "Remove the service and program files in $DIR (config.json is kept)?" n || die "cancelled"
    fi
    if have_systemd && service_exists; then
        systemctl disable --now "$SERVICE" 2>/dev/null || true
        rm -f "$SYSTEMD_DIR/$SERVICE.service"
        systemctl daemon-reload
    else
        rm -f "$SYSTEMD_DIR/$SERVICE.service"
    fi
    if [ "$PURGE" -eq 1 ]; then
        rm -rf "$DIR"
        id -u "$SVC_USER" >/dev/null 2>&1 && userdel "$SVC_USER" 2>/dev/null || true
        say "Removed everything."
    else
        find "$DIR" -mindepth 1 -not -name config.json -not -name config.json.bak -delete
        say "Removed the program. Your settings are still in $DIR/config.json."
    fi
}

case "$CMD" in
    install)   do_install ;;
    update)    do_update ;;
    uninstall) do_uninstall ;;
esac
