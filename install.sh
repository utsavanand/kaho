#!/bin/zsh
set -euo pipefail

[[ "$(uname -m)" == "arm64" ]] || { echo "Sotto requires Apple Silicon (transcription runs on MLX)"; exit 1; }

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
SUPPORT="$HOME/Library/Application Support/Sotto"
APP="/Applications/Sotto.app"
STAGE="/Applications/.Sotto.app.new"

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

echo "building $APP ..."
# Stage the new bundle completely before touching the existing app, so a
# failed build never destroys a working installation
rm -rf "$STAGE"
mkdir -p "$STAGE/Contents/MacOS" "$STAGE/Contents/Resources"
cp "$SRC/sotto.py" "$STAGE/Contents/Resources/"
cp "$SRC/assets/Sotto.icns" "$STAGE/Contents/Resources/"

cat > "$STAGE/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Sotto</string>
  <key>CFBundleDisplayName</key><string>Sotto</string>
  <key>CFBundleIdentifier</key><string>com.utsavanand.sotto</string>
  <key>CFBundleExecutable</key><string>sotto</string>
  <key>CFBundleIconFile</key><string>Sotto</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.7.8</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSMicrophoneUsageDescription</key><string>Sotto records while you hold the hotkey and transcribes on-device.</string>
</dict>
</plist>
PLIST

cat > "$STAGE/Contents/MacOS/sotto" <<LAUNCH
#!/bin/zsh
exec "$SUPPORT/venv/bin/python" "\$(cd "\$(dirname "\$0")/../Resources" && pwd)/sotto.py"
LAUNCH
chmod +x "$STAGE/Contents/MacOS/sotto"

# Ad-hoc signature: local install needs no notarization, and a signature gives
# the bundle a stabler TCC identity than none at all
codesign --force -s - "$STAGE"
rm -rf "$APP"
mv "$STAGE" "$APP"

echo ""
echo "done. launch with:  open /Applications/Sotto.app"
echo "then grant Sotto in System Settings > Privacy & Security:"
echo "  Microphone and Accessibility — and relaunch."
echo "log file: ~/Library/Logs/Sotto.log (also in the menu bar: 🎙 > Open Log)"
