#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

python_bin="${SEEKBAR_PYTHON:-python3}"
app_version="${SEEKBAR_VERSION:-0.1.5}"
arch="$(uname -m)"
signing_dir="$HOME/Library/Application Support/SeekBar Signing"
keychain="$signing_dir/identity.keychain-db"
if [[ ! -s "$signing_dir/fingerprint" || ! -f "$keychain" || ! -s "$signing_dir/keychain-password" ]]; then
    printf 'SeekBar signing identity missing. Use ./setup-local-signing.sh for the first setup or restore the original identity.\n' >&2
    exit 1
fi
fingerprint="$(<"$signing_dir/fingerprint")"
if [[ ! "$fingerprint" =~ ^[[:xdigit:]]{40}$ ]]; then
    printf 'Invalid SeekBar signing certificate fingerprint.\n' >&2
    exit 1
fi
case "$arch" in
    arm64|x86_64) ;;
    *) echo "Unsupported macOS architecture: $arch" >&2; exit 1 ;;
esac

if [[ ! -x .venv/bin/python ]]; then
    "$python_bin" -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -r requirements-build.txt
python_executable="$(.venv/bin/python -c 'import sys; print(sys._base_executable)')"
macos_min="$(vtool -show-build "$python_executable" | awk '/minos/ {print $2; exit}')"
export SEEKBAR_VERSION="$app_version" SEEKBAR_ARCH="$arch" SEEKBAR_MIN_MACOS="${macos_min:-11.0}"
stage="$(mktemp -d -t seekbar-build)"
keychain_unlocked=false
cleanup() {
    if [[ "$keychain_unlocked" == true ]]; then
        /usr/bin/security lock-keychain "$keychain" || true
    fi
    rm -rf -- "$stage"
}
trap cleanup EXIT

.venv/bin/python -m PyInstaller --noconfirm --distpath "$stage/dist" --workpath "$stage/work" SeekBar.spec
app="$stage/dist/SeekBar.app"
/usr/bin/security unlock-keychain -p "$(<"$signing_dir/keychain-password")" "$keychain"
keychain_unlocked=true
/usr/bin/codesign --force --sign "$fingerprint" --keychain "$keychain" --timestamp=none "$app"
/usr/bin/security lock-keychain "$keychain"
keychain_unlocked=false
requirement="identifier \"local.seekbar.launcher\" and certificate leaf = H\"$fingerprint\""
/usr/bin/codesign --verify --deep --strict "$app"
/usr/bin/codesign --verify --strict -R "=$requirement" "$app"

package="SeekBar-${app_version}-${arch}.pkg"
/usr/bin/productbuild --component "$app" /Applications --version "$app_version" "$stage/$package"
/usr/sbin/pkgutil --payload-files "$stage/$package" | /usr/bin/grep -Ex '(\./)?SeekBar\.app/Contents/MacOS/SeekBar'
/usr/sbin/pkgutil --expand "$stage/$package" "$stage/package-info"
.venv/bin/python - "$stage/package-info/local.seekbar.launcher.pkg/PackageInfo" <<'PYTHON'
import sys
import xml.etree.ElementTree as ET

receipt = ET.parse(sys.argv[1]).getroot()
bundle_id = "local.seekbar.launcher"
upgrade = receipt.find(f"upgrade-bundle/bundle[@id='{bundle_id}']")
bundle = receipt.find(f"bundle[@id='{bundle_id}']")
if (
    receipt.get("identifier") != bundle_id
    or receipt.get("install-location") != "/Applications"
    or receipt.get("relocatable") != "false"
    or upgrade is None
    or bundle is None
    or bundle.get("path") not in ("SeekBar.app", "./SeekBar.app")
):
    raise SystemExit("Installer must replace /Applications/SeekBar.app in place.")
PYTHON
mkdir -p dist
mv -f "$stage/$package" "dist/$package"
printf 'Installer created: %s/dist/%s\n' "$PWD" "$package"
