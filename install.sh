#!/bin/zsh
set -euo pipefail

[[ "$(uname -m)" == "arm64" ]] || { echo "Kaho requires Apple Silicon (transcription runs on MLX)"; exit 1; }

# Find a 3.13 interpreter by its versioned name first: after
# `brew install python@3.13` the unversioned `python3` on PATH is often a
# different version (Apple's /usr/bin/python3, or Homebrew's newer default)
PY=""
for cand in python3.13 python3; do
  if command -v "$cand" >/dev/null \
     && "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)' 2>/dev/null; then
    PY="$(command -v "$cand")"
    break
  fi
done
[[ -n "$PY" ]] || { echo "python 3.13 not found (the hashed lock file pins 3.13 wheels) — install with: brew install python@3.13"; exit 1; }
echo "using $PY"

SRC="$(cd "$(dirname "$0")" && pwd)"
# Read the version out of kaho.py rather than keeping a second copy here
VERSION="$(sed -nE 's/^APP_VERSION = "([^"]+)".*/\1/p' "$SRC/kaho.py")"
[[ -n "$VERSION" ]] || { echo "could not read APP_VERSION from kaho.py"; exit 1; }
SUPPORT="$HOME/Library/Application Support/Kaho"
APP="/Applications/Kaho.app"
STAGE="/Applications/.Kaho.app.new"

# The app was named Sotto through 1.7.x. Carry settings, history and the
# dictionary over before creating $SUPPORT — once it exists the app's own
# migration correctly declines to touch it, and the old data is stranded.
OLD_SUPPORT="$HOME/Library/Application Support/Sotto"
if [[ -d "$OLD_SUPPORT" && ! -d "$SUPPORT" ]]; then
  echo "carrying settings and history over from Sotto ..."
  mv "$OLD_SUPPORT" "$SUPPORT"
  # The old venv points at the old path in its own config and scripts
  rm -rf "$SUPPORT/venv"
fi

echo "installing python environment into $SUPPORT ..."
mkdir -p "$SUPPORT"
# A venv left behind by an older release may be a pre-3.13 Python whose
# wheels don't match the hashed lock — recreate it rather than reuse it
if [[ -x "$SUPPORT/venv/bin/python" ]]; then
  "$SUPPORT/venv/bin/python" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)' \
    || { echo "recreating venv (old Python version)"; rm -rf "$SUPPORT/venv"; }
fi
[[ -x "$SUPPORT/venv/bin/python" ]] || "$PY" -m venv "$SUPPORT/venv"
# Hash-verified, fully pinned install: a compromised upstream release can't
# slip into an app that holds mic + Accessibility permissions
"$SUPPORT/venv/bin/pip" install --quiet --require-hashes --no-deps --timeout 60 --retries 10 -r "$SRC/requirements.lock"

echo "building $APP $VERSION ..."
# Stage the new bundle completely before touching the existing app, so a
# failed build never destroys a working installation
rm -rf "$STAGE"
mkdir -p "$STAGE/Contents/MacOS" "$STAGE/Contents/Resources"
cp "$SRC/kaho.py" "$STAGE/Contents/Resources/"
cp "$SRC/assets/Kaho.icns" "$STAGE/Contents/Resources/"

cat > "$STAGE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Kaho</string>
  <key>CFBundleDisplayName</key><string>Kaho</string>
  <key>CFBundleIdentifier</key><string>com.utsavanand.kaho</string>
  <key>CFBundleExecutable</key><string>kaho</string>
  <key>CFBundleIconFile</key><string>Kaho</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSMicrophoneUsageDescription</key><string>Kaho records while you hold the hotkey and transcribes on-device.</string>
</dict>
</plist>
PLIST

cat > "$STAGE/Contents/MacOS/kaho" <<LAUNCH
#!/bin/zsh
exec "$SUPPORT/venv/bin/python" "\$(cd "\$(dirname "\$0")/../Resources" && pwd)/kaho.py"
LAUNCH
chmod +x "$STAGE/Contents/MacOS/kaho"

# Ad-hoc signature: local install needs no notarization, and a signature gives
# the bundle a stabler TCC identity than none at all
codesign --force -s - "$STAGE"
rm -rf "$APP"
mv "$STAGE" "$APP"

echo ""
echo "done. launch with:  open /Applications/Kaho.app"
echo "then grant Kaho in System Settings > Privacy & Security:"
echo "  Microphone and Accessibility — and relaunch."
echo "log file: ~/Library/Logs/Kaho.log (also in the menu bar: 🎙 > Open Log)"
