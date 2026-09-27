# Task: Homebrew tap for Sotto

Create a Homebrew tap so people can install Sotto with one command.

1. Create a public GitHub repo `utsavanand/homebrew-sotto`
   (verify auth first: `gh api user --jq .login` should print utsavanand).
2. Add `Casks/sotto.rb` — a cask for Sotto 1.7.8:
   - url "https://github.com/utsavanand/sotto/releases/download/v1.7.8/Sotto-1.7.8.dmg"
   - sha256: download the asset and compute it (`shasum -a 256`)
   - version "1.7.8"
   - name "Sotto"; desc "Local voice dictation for macOS — hold a key, speak, release"
   - homepage "https://sotto.utsava.xyz"
   - depends_on macos: ">= :sonoma" (app is Apple Silicon only)
   - app "Sotto.app"
   - zap trash: ["~/Library/Application Support/Sotto", "~/Library/Logs/Sotto.log"]
   - livecheck against the GitHub releases page
3. Validate: `brew style` on the cask, and `brew audit --cask ./Casks/sotto.rb`
   if brew exists in your environment. If you cannot run brew, SAY SO in your
   report — do not skip silently.
4. README: install is `brew install --cask utsavanand/sotto/sotto`.
5. Push, then report what you created, what you verified, and what you could not.

The DMG is signed and notarized — no quarantine caveats needed.
Do NOT modify the main sotto repo; work only in homebrew-sotto.
