#!/bin/zsh
# Import a Developer ID certificate and verify the machine can sign.
#
# Run this after downloading developerID_application.cer from
# developer.apple.com. It pairs the certificate with the private key that
# generated the CSR, which is the step that silently fails when people
# download a certificate onto a machine that never made the request.
#
#   ./packaging/setup-signing.sh ~/Downloads/developerID_application.cer

set -euo pipefail

CER="${1:-}"
KEYDIR="$HOME/Desktop/kaho-signing"
KEY="$KEYDIR/DeveloperID.key"

if [[ -z "$CER" ]]; then
    echo "usage: $0 <path-to-developerID_application.cer>"
    echo ""
    echo "Get one at developer.apple.com > Certificates > + > Developer ID Application,"
    echo "uploading $KEYDIR/DeveloperID.certSigningRequest when asked."
    exit 1
fi
[[ -f "$CER" ]] || { echo "no such file: $CER"; exit 1; }
[[ -f "$KEY" ]] || { echo "missing private key at $KEY — it must be the one that made the CSR"; exit 1; }

echo "==> importing the certificate"
security import "$CER" -k ~/Library/Keychains/login.keychain-db 2>&1 | grep -v "already exists" || true

echo "==> importing the private key"
security import "$KEY" -k ~/Library/Keychains/login.keychain-db \
    -T /usr/bin/codesign -T /usr/bin/security 2>&1 | grep -v "already exists" || true

echo "==> checking"
if security find-identity -v -p codesigning | grep -q "Developer ID Application"; then
    echo ""
    security find-identity -v -p codesigning | grep "Developer ID Application"
    echo ""
    echo "Signing is ready. Next, store notarization credentials once:"
    echo ""
    echo "  xcrun notarytool store-credentials sotto-notary \\"
    # The Apple Developer account, which is not the address used for bug
    # reports — storing the profile under the wrong one validates at the time
    # and then fails every lookup later with "no Keychain password item found"
    echo "    --apple-id \"pingutsav@gmail.com\" \\"
    # The certificate's common name ends in "(TEAMID)", so the team id does
    # not have to be looked up by hand
    TEAM_ID="$(security find-identity -v -p codesigning \
        | grep "Developer ID Application" | head -1 \
        | sed -E 's/.*\(([A-Z0-9]+)\)".*/\1/')"
    echo "    --team-id \"${TEAM_ID:-<TEAM_ID>}\" \\"
    echo "    --password \"<app-specific-password>\""
    echo ""
    echo "App-specific password: appleid.apple.com > Sign-In and Security."
    echo "Then:  ./packaging/release.sh <version>"
else
    echo ""
    echo "No Developer ID Application identity yet."
    echo "Most likely the certificate was issued against a different CSR than"
    echo "$KEY. Create a new certificate using that CSR and re-run this."
    exit 1
fi
