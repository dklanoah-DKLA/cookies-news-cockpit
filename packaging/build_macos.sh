#!/usr/bin/env bash
# Build, validate and package the Intel macOS application.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
APP_NAME="Cookies News Cockpit"
APP_PATH="${PROJECT_ROOT}/dist/${APP_NAME}.app"
EXECUTABLE_PATH="${APP_PATH}/Contents/MacOS/${APP_NAME}"
APP_VERSION="${APP_VERSION:-0.1.0}"
export APP_VERSION
export MACOSX_DEPLOYMENT_TARGET="14.0"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: this build must run on macOS" >&2
  exit 1
fi

if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "error: expected an Intel x86_64 runner, got $(uname -m)" >&2
  exit 1
fi

if [[ -z "${PROJECT_ROOT}" || "${PROJECT_ROOT}" == "/" || ! -f "${PROJECT_ROOT}/pyproject.toml" ]]; then
  echo "error: refusing to clean an unresolved project directory" >&2
  exit 1
fi

rm -rf -- "${PROJECT_ROOT}/build" "${PROJECT_ROOT}/dist" "${PROJECT_ROOT}/release"
mkdir -p -- "${PROJECT_ROOT}/build" "${PROJECT_ROOT}/release"

cd -- "${PROJECT_ROOT}"
python "packaging/generate_icon.py" \
  --output "${PROJECT_ROOT}/build/CookiesNewsCockpit.icns" \
  --work-dir "${PROJECT_ROOT}/build/CookiesNewsCockpit.iconset"
python -m PyInstaller --noconfirm --clean "packaging/CookiesNewsCockpit.spec"

if [[ ! -d "${APP_PATH}" || ! -x "${EXECUTABLE_PATH}" ]]; then
  echo "error: PyInstaller did not create the expected app bundle" >&2
  exit 1
fi

# There are deliberately no Apple Developer credentials in V1. The dash creates
# an ad-hoc signature for bundle integrity; it does not notarize the application.
xattr -cr "${APP_PATH}"
codesign --force --deep --sign - --timestamp=none "${APP_PATH}"
codesign --verify --deep --strict --verbose=2 "${APP_PATH}"

if ! lipo -archs "${EXECUTABLE_PATH}" | tr ' ' '\n' | grep -qx "x86_64"; then
  echo "error: main executable is not x86_64" >&2
  lipo -info "${EXECUTABLE_PATH}" >&2 || true
  exit 1
fi

while IFS= read -r -d '' candidate; do
  if file -b "${candidate}" | grep -q "Mach-O"; then
    if ! lipo -archs "${candidate}" | tr ' ' '\n' | grep -qx "x86_64"; then
      echo "error: bundled Mach-O file lacks x86_64: ${candidate}" >&2
      lipo -info "${candidate}" >&2 || true
      exit 1
    fi
  fi
done < <(find "${APP_PATH}" -type f -print0)

APP_MINIMUM_SYSTEM="$(/usr/libexec/PlistBuddy -c 'Print :LSMinimumSystemVersion' "${APP_PATH}/Contents/Info.plist")"
APP_BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "${APP_PATH}/Contents/Info.plist")"

if [[ "${APP_MINIMUM_SYSTEM}" != "14.0" ]]; then
  echo "error: unexpected minimum macOS version: ${APP_MINIMUM_SYSTEM}" >&2
  exit 1
fi

if [[ "${APP_BUNDLE_ID}" != "com.cookies.newscockpit" ]]; then
  echo "error: unexpected bundle identifier: ${APP_BUNDLE_ID}" >&2
  exit 1
fi

bash "${SCRIPT_DIR}/package_dmg.sh" "${APP_PATH}" "${PROJECT_ROOT}/release" "${APP_VERSION}"

echo "Built ${APP_PATH}"
echo "Release artifacts:"
find "${PROJECT_ROOT}/release" -maxdepth 1 -type f -print
