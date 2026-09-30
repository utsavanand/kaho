#!/bin/zsh
# Build, sign, notarize, and package Kaho.app as a distributable DMG.
#
# Unlike install.sh (which builds against the user's own Python and is not
# distributable), this produces a self-contained bundle for people who will
# never open a terminal.
#
# One-time setup:
#   1. developer.apple.com -> Certificates -> "+" -> Developer ID Application
#      Install the downloaded .cer by double-clicking it.
#   2. appleid.apple.com -> Sign-In and Security -> App-Specific Passwords
#   3. xcrun notarytool store-credentials sotto-notary \
#        --apple-id "you@example.com" --team-id "TEAMID" --password "app-specific-password"
#
# Then:  ./packaging/release.sh 2.0.0

set -euo pipefail

VERSION="${1:-}"
[[ -n "$VERSION" ]] || { echo "usage: $0 <version>   e.g. $0 2.0.0"; exit 1; }

SRC="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$SRC/build-release"
# The keychain profile keeps its original name: renaming it would require
# re-entering the app-specific password for zero benefit.
NOTARY_PROFILE="${KAHO_NOTARY_PROFILE:-sotto-notary}"

# Resolve the Developer ID automatically: hardcoding it means every machine
# needs an edit, and the hash changes when the certificate is renewed
IDENTITY="$(security find-identity -v -p codesigning \
    | grep "Developer ID Application" \
    | head -1 \
    | sed -E 's/.*"(.*)"/\1/')"
if [[ -z "$IDENTITY" ]]; then
    echo "No 'Developer ID Application' certificate found in the keychain."
    echo "Create one at developer.apple.com > Certificates, then double-click the .cer."
    exit 1
fi
echo "signing as: $IDENTITY"

echo "==> building the bundle"
rm -rf "$BUILD"
mkdir -p "$BUILD"
KAHO_VERSION="$VERSION" "$SRC/.venv/bin/pyinstaller" "$SRC/packaging/Kaho.spec" \
    --noconfirm --distpath "$BUILD/dist" --workpath "$BUILD/work" >/dev/null

APP="$BUILD/dist/Kaho.app"
[[ -d "$APP" ]] || { echo "build produced no app bundle"; exit 1; }

echo "==> signing"
# The hardened runtime is required for notarization. Python loads compiled
# extensions at runtime, which the runtime blocks without these entitlements.
cat > "$BUILD/entitlements.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
  <key>com.apple.security.cs.disable-library-validation</key><true/>
  <key>com.apple.security.device.audio-input</key><true/>
</dict>
</plist>
PLIST

# Sign inner binaries before the bundle: codesign requires depth-first order.
#
# Retried, and NOT silenced. --timestamp contacts Apple's timestamp server for
# every binary, and a single refused connection produces one dylib signed
# without a timestamp. That used to be swallowed by `2>/dev/null || true`, so
# the run continued, the outer signature was applied over an inner binary that
# was later re-signed, and the verify failed with "a timestamp was expected"
# on a bundle that could no longer be repaired in place.
sign_inner() {
    find "$APP/Contents" \( -name "*.so" -o -name "*.dylib" \) -print0 \
        | xargs -0 -P 4 -I {} codesign --force --timestamp --options runtime \
            --entitlements "$BUILD/entitlements.plist" --sign "$IDENTITY" {}
}
for attempt in 1 2 3; do
    if sign_inner; then
        break
    fi
    if [[ $attempt == 3 ]]; then
        echo "inner binaries could not be signed after 3 attempts — see the errors above"
        exit 1
    fi
    echo "signing failed (likely the timestamp server) — retrying in 15s"
    sleep 15
done

codesign --force --deep --timestamp --options runtime \
    --entitlements "$BUILD/entitlements.plist" --sign "$IDENTITY" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

echo "==> packaging the dmg"
DMG="$BUILD/Kaho-$VERSION.dmg"
STAGE="$BUILD/stage"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"   # drag-to-install target
# The volume name is deliberately not the bare app name: hdiutil mounts the
# image at /Volumes/<volname> while building, and under the old name something
# on this machine held a claim on /Volumes/Sotto that survived a detach — every
# attempt failed with a bare "Operation not permitted" naming no cause.
# "Kaho Installer" also reads better in the Finder title bar.
hdiutil detach "/Volumes/Kaho Installer" -force >/dev/null 2>&1 || true
hdiutil create -volname "Kaho Installer" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
codesign --force --timestamp --sign "$IDENTITY" "$DMG"

echo "==> notarizing (usually minutes; large uploads can take an hour)"
# Submit and wait separately. `submit --wait` died mid-wait when the script
# ran detached, leaving an unstapled DMG that looked like a success. Capturing
# the id first means the wait can be retried without re-uploading 350+ MB.
#
# The submission goes to a file rather than straight into $( ): notarytool has
# twice been killed partway through while the script was backgrounded, and a
# command substitution swallows whatever it had printed, so the id — and the
# upload it represents — was lost even though Apple had accepted the bytes.
SUBMIT_OUT="$BUILD/notarytool-submit.txt"
xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" \
    2>&1 | tee "$SUBMIT_OUT"
SUBMIT_ID="$(awk '/^  id:/ {print $2; exit}' "$SUBMIT_OUT")"
if [[ -z "$SUBMIT_ID" ]]; then
    echo ""
    echo "No submission id. notarytool output is in $SUBMIT_OUT."
    echo "If the upload did complete, find the id with:"
    echo "  xcrun notarytool history --keychain-profile $NOTARY_PROFILE"
    echo "then resume without re-uploading:"
    echo "  xcrun notarytool wait <id> --keychain-profile $NOTARY_PROFILE"
    echo "  xcrun stapler staple \"$DMG\""
    exit 1
fi
echo "submission id: $SUBMIT_ID"
xcrun notarytool wait "$SUBMIT_ID" --keychain-profile "$NOTARY_PROFILE"

STATUS="$(xcrun notarytool info "$SUBMIT_ID" --keychain-profile "$NOTARY_PROFILE" \
    | awk '/^  status:/ {print $2; exit}')"
if [[ "$STATUS" != "Accepted" ]]; then
    echo "notarization $STATUS — details:"
    xcrun notarytool log "$SUBMIT_ID" --keychain-profile "$NOTARY_PROFILE"
    exit 1
fi

echo "==> stapling"
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"

echo ""
echo "done: $DMG"
echo "verify a clean install with:  spctl -a -t open --context context:primary-signature -v \"$DMG\""
