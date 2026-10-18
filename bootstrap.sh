#!/usr/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -eu
changed_ro=0
tmp=
cleanup() {
    result=$?
    if [ "$changed_ro" = 1 ]; then sudo steamos-readonly enable || result=1; fi
    if [ -n "$tmp" ]; then rm -f -- "$tmp"; fi
    echo
    echo "Bosun installer finished (status $result). Press Enter to close."
    read -r answer || true
    exit "$result"
}
trap cleanup EXIT
missing=()
command -v python3 >/dev/null || missing+=(python)
command -v curl >/dev/null || missing+=(curl)
if [ "${#missing[@]}" -gt 0 ]; then
    command -v pacman >/dev/null || { echo 'SteamOS package manager unavailable'; exit 1; }
    readonly_before=$(steamos-readonly status 2>/dev/null || true)
    if [ "${readonly_before%%,*}" = enabled ]; then
        sudo steamos-readonly disable
        changed_ro=1
    fi
    planned=$(sudo pacman -Sp --print-format '%n %v' "${missing[@]}")
    [ -n "$planned" ] || { echo 'Empty dependency transaction'; exit 1; }
    while read -r package version; do
        [[ "$package" =~ ^[a-zA-Z0-9@+_.-]+$ ]] || exit 1
        installed=$(pacman -Q "$package" 2>/dev/null || true)
        if [ -n "$installed" ] && [ "$installed" != "$package $version" ]; then
            echo "Installing tools would change installed $package. Update SteamOS first."
            exit 1
        fi
    done <<< "$planned"
    sudo pacman -S --noconfirm "${missing[@]}"
    if [ "$changed_ro" = 1 ]; then sudo steamos-readonly enable; changed_ro=0; fi
fi
tmp=$(mktemp /tmp/bosun-installer-XXXXXX.py)
curl --fail --location --proto '=https' --proto-redir '=https' --connect-timeout 20 --max-time 120 --output "$tmp" https://raw.githubusercontent.com/ISAIandCO/Bosun/main/install.py
python3 "$tmp"
