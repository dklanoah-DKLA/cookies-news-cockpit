#!/usr/bin/env bash
# Turn a validated .app into a compact DMG plus a WeChat-friendly ZIP wrapper.
set -Eeuo pipefail

if [[ "$#" -ne 3 ]]; then
  echo "usage: $0 <app-path> <output-directory> <version>" >&2
  exit 64
fi

APP_PATH="$(cd -- "$(dirname -- "$1")" && pwd -P)/$(basename -- "$1")"
OUTPUT_DIR="$2"
VERSION="$3"
APP_NAME="Cookies News Cockpit"
SAFE_VERSION="$(printf '%s' "${VERSION}" | tr -cd '0-9A-Za-z._-')"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: DMG packaging requires macOS" >&2
  exit 1
fi

if [[ ! -d "${APP_PATH}" || "${APP_PATH##*.}" != "app" ]]; then
  echo "error: expected an existing .app bundle: ${APP_PATH}" >&2
  exit 1
fi

if [[ -z "${SAFE_VERSION}" || "${SAFE_VERSION}" != "${VERSION}" ]]; then
  echo "error: version may contain only letters, digits, dots, underscores and hyphens" >&2
  exit 1
fi

mkdir -p -- "${OUTPUT_DIR}"
OUTPUT_DIR="$(cd -- "${OUTPUT_DIR}" && pwd -P)"
STAGING_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cookies-news-cockpit-dmg.XXXXXX")"
DMG_PATH="${OUTPUT_DIR}/Cookies-News-Cockpit-${SAFE_VERSION}-Intel.dmg"
ZIP_PATH="${DMG_PATH}.zip"

cleanup() {
  rm -rf -- "${STAGING_DIR}"
}
trap cleanup EXIT

ditto "${APP_PATH}" "${STAGING_DIR}/${APP_NAME}.app"
ln -s /Applications "${STAGING_DIR}/Applications"

rm -f -- "${DMG_PATH}" "${ZIP_PATH}" "${OUTPUT_DIR}/SHA256SUMS.txt"
hdiutil create \
  -volname "${APP_NAME}" \
  -srcfolder "${STAGING_DIR}" \
  -ov \
  -format UDZO \
  -imagekey zlib-level=9 \
  "${DMG_PATH}"

# The ZIP does not materially shrink a compressed DMG. It is provided because
# some chat clients handle a .zip attachment more reliably than a raw .dmg.
ditto -c -k --keepParent "${DMG_PATH}" "${ZIP_PATH}"

(
  cd -- "${OUTPUT_DIR}"
  shasum -a 256 "$(basename -- "${DMG_PATH}")" "$(basename -- "${ZIP_PATH}")" > SHA256SUMS.txt
)

hdiutil verify "${DMG_PATH}"
