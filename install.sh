#!/usr/bin/env bash
set -euo pipefail

OWNER="Gazman-Dev"
REPO="tele-cli"
BRANCH="${TELE_CLI_INSTALL_BRANCH:-master}"
SETUP_URL="https://raw.githubusercontent.com/${OWNER}/${REPO}/${BRANCH}/setup.sh"
CACHE_BUSTER="$(date +%s)-$$"
TMP_SCRIPT="$(mktemp "${TMPDIR:-/tmp}/tele-cli-setup.XXXXXX.sh")"

cleanup() {
  rm -f "$TMP_SCRIPT"
}

trap cleanup EXIT

curl \
  -fsSL \
  -H "Cache-Control: no-cache" \
  -H "Pragma: no-cache" \
  "${SETUP_URL}?source=install-wrapper&ts=${CACHE_BUSTER}" \
  -o "$TMP_SCRIPT"

exec bash "$TMP_SCRIPT" "$@"
