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
#   3. xcrun notarytool store-credentials kaho-notary \
#        --apple-id "you@example.com" --team-id "TEAMID" --password "app-specific-password"
#
# Then:  ./packaging/release.sh 2.0.0

set -euo pipefail

VERSION="${1:-}"
[[ -n "$VERSION" ]] || { echo "usage: $0 <version>   e.g. $0 2.0.0"; exit 1; }

SRC="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$SRC/build-release"
# Named for this app, not shared. The profile was called "sotto-notary" through
# 2.1.0, which another project on this machine also used — each store-credentials
# overwrote the other, and notarization then failed with "No Keychain password
# item found" on a profile that had existed minutes earlier.
NOTARY_PROFILE="${KAHO_NOTARY_PROFILE:-kaho-notary}"

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

# PyInstaller bundles whatever is importable in the venv, not what the lock
# says. A package the lock dropped stays installed until something removes
# it — 2.2.0 replaced Whisper with mlx-audio, and a stale torch left behind
# from before silently went on shipping, making the app 575 MB larger than
# the release notes claimed.
echo "==> checking the build venv matches the lock"
EXTRA="$("$SRC/.venv/bin/pip" list --format=freeze 2>/dev/null \
    | cut -d= -f1 | tr 'A-Z_' 'a-z-' | sort -u \
    | comm -23 - <(grep -oE '^[A-Za-z0-9._-]+' "$SRC/requirements.lock" \
        | tr 'A-Z_' 'a-z-' | sort -u) \
    | grep -vxE 'pip|setuptools|wheel|pyinstaller|pyinstaller-hooks-contrib|ruff|altgraph|macholib|packaging|pefile' || true)"
if [[ -n "$EXTRA" ]]; then
    echo "The build venv has packages the lock does not list:"
    echo "$EXTRA" | sed 's/^/  /'
    echo ""
    echo "PyInstaller will bundle them. Recreate the venv:"
    echo "  rm -rf .venv && python3.13 -m venv .venv"
    echo "  .venv/bin/pip install --require-hashes --no-deps -r requirements.lock"
    echo "  .venv/bin/pip install pyinstaller ruff"
    exit 1
fi

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

# Every Mach-O in the bundle must run on the OS the bundle claims to support.
# 2.2.0 shipped declaring macOS 14 with 64 binaries built for 15 — Homebrew's
# Python and its libraries are compiled for the build host. LaunchServices
# accepted the app and dyld then failed to load Python, so it died before
# main() with no message and no permission prompt. A tester saw a dead icon.
echo "==> checking the deployment target"
DECLARED="$(/usr/libexec/PlistBuddy -c 'Print LSMinimumSystemVersion' "$APP/Contents/Info.plist")"
TOO_NEW="$(find "$APP" -type f \( -name '*.so' -o -name '*.dylib' -o -perm +111 \) -print0 \
    | xargs -0 -I {} sh -c '
        file "$1" 2>/dev/null | grep -q Mach-O || exit 0
        m=$(vtool -show-build "$1" 2>/dev/null | awk "/minos/{print \$2; exit}")
        [ -n "$m" ] && printf "%s %s\n" "$m" "$1"
      ' _ {} \
    | awk -v want="$DECLARED" '{
        split($1, a, "."); split(want, b, ".")
        if (a[1] > b[1] || (a[1] == b[1] && a[2] > b[2])) print
      }')"
if [[ -n "$TOO_NEW" ]]; then
    echo "The bundle declares macOS $DECLARED but contains binaries that need more:"
    printf '%s\n' "$TOO_NEW" | sed 's|.*/Kaho.app/|  |' | sort -u | head -20
    echo ""
    echo "Total: $(printf '%s\n' "$TOO_NEW" | wc -l | tr -d ' ') binaries."
    echo "Usually the venv was built with Homebrew's Python, which targets the"
    echo "build host. Rebuild it with python.org's universal2 Python:"
    echo "  rm -rf .venv"
    echo "  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -m venv .venv"
    echo "  .venv/bin/pip install --require-hashes --no-deps -r requirements.lock"
    echo "  .venv/bin/pip install pyinstaller ruff"
    exit 1
fi
echo "all binaries run on macOS $DECLARED"

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

# Poll with short-lived `info` calls rather than one long `notarytool wait`.
# The keychain credential vanished three times in one day, each time while a
# long-running wait was alive, and the keychain's own mtime matched. The
# cause is not proven, but a wait that outlives the shell holds a session
# open for an hour or more and is the only thing correlated with it — and
# polling costs nothing. It also means a lost credential is reported as
# such rather than silently reading as "still in progress" forever.
await_notarization() {
    local id="$1" waited=0 status=""
    while (( waited < 3600 )); do
        status="$(xcrun notarytool info "$id" --keychain-profile "$NOTARY_PROFILE" 2>&1 \
            | /usr/bin/awk '/^  status:/ {print $2; exit}')"
        case "$status" in
            Accepted) return 0 ;;
            Invalid|Rejected)
                echo "notarization $status — details:"
                xcrun notarytool log "$id" --keychain-profile "$NOTARY_PROFILE"
                return 1 ;;
            "")
                echo "could not read submission $id — is the keychain profile still there?"
                echo "  xcrun notarytool store-credentials $NOTARY_PROFILE ..."
                return 1 ;;
        esac
        sleep 30
        (( waited += 30 ))
    done
    echo "still In Progress after an hour: $id"
    echo "resume with:  xcrun notarytool info $id --keychain-profile $NOTARY_PROFILE"
    return 1
}

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
    echo "  xcrun notarytool info <id> --keychain-profile $NOTARY_PROFILE"
    echo "  xcrun stapler staple \"$DMG\""
    exit 1
fi
echo "submission id: $SUBMIT_ID"
await_notarization "$SUBMIT_ID" || exit 1

STATUS="$(xcrun notarytool info "$SUBMIT_ID" --keychain-profile "$NOTARY_PROFILE" \
    | awk '/^  status:/ {print $2; exit}')"
if [[ "$STATUS" != "Accepted" ]]; then
    echo "notarization $STATUS — details:"
    xcrun notarytool log "$SUBMIT_ID" --keychain-profile "$NOTARY_PROFILE"
    exit 1
fi

echo "==> stapling"
# Both the app and the DMG, and the app FIRST — then the image is rebuilt
# around the stapled copy. Notarizing the DMG approves the app inside it too,
# but the ticket only lands on whatever stapler is pointed at: shipping only
# the DMG ticket leaves the installed Kaho.app relying on an online check,
# so a first launch offline or behind a slow network stalls on Gatekeeper.
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"

rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil detach "/Volumes/Kaho Installer" -force >/dev/null 2>&1 || true
rm -f "$DMG"
hdiutil create -volname "Kaho Installer" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
codesign --force --timestamp --sign "$IDENTITY" "$DMG"

# The rebuilt image is a new file, so it needs its own trip through the
# notary service; the app inside already carries its ticket.
xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" \
    2>&1 | tee "$BUILD/notarytool-dmg.txt"
DMG_ID="$(awk '/^  id:/ {print $2; exit}' "$BUILD/notarytool-dmg.txt")"
[[ -n "$DMG_ID" ]] || { echo "no submission id for the rebuilt dmg"; exit 1; }
await_notarization "$DMG_ID" || exit 1
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"

echo ""
echo "done: $DMG"
echo "verify a clean install with:  spctl -a -t open --context context:primary-signature -v \"$DMG\""
