# Task: Homebrew tap for Kaho

Create a Homebrew tap so people can install Kaho with one command.

1. Create a public GitHub repo `utsavanand/homebrew-kaho`
   (verify auth first: `gh api user --jq .login` should print utsavanand).
2. Add `Casks/kaho.rb` — a cask for Kaho 1.7.8:
   - url "https://github.com/utsavanand/kaho/releases/download/v1.7.8/Kaho-1.7.8.dmg"
   - sha256: download the asset and compute it (`shasum -a 256`)
   - version "1.7.8"
   - name "Kaho"; desc "Local voice dictation for macOS — hold a key, speak, release"
   - homepage "https://kaho.utsava.xyz"
   - depends_on macos: ">= :sonoma" (app is Apple Silicon only)
   - app "Kaho.app"
   - zap trash: ["~/Library/Application Support/Kaho", "~/Library/Logs/Kaho.log"]
   - livecheck against the GitHub releases page
3. Validate: `brew style` on the cask, and `brew audit --cask ./Casks/kaho.rb`
   if brew exists in your environment. If you cannot run brew, SAY SO in your
   report — do not skip silently.
4. README: install is `brew install --cask utsavanand/kaho/kaho`.
5. Push, then report what you created, what you verified, and what you could not.

The DMG is signed and notarized — no quarantine caveats needed.
Do NOT modify the main kaho repo; work only in homebrew-kaho.
