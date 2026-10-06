KAHO FOR WINDOWS — TEST BUILD (milestone 1)
============================================

This is a rough prototype, not the app. It proves three things on your PC:
hold-to-talk works, the text lands where your cursor is, and how fast the
speech engines are on your hardware.

1. Unzip this folder anywhere (for example Desktop\kaho-spike).

2. Double-click kaho-spike.exe.
   Windows will show "Windows protected your PC" because this test build is
   not code-signed yet. Click "More info", then "Run anyway".

   A black console window opens. The FIRST run downloads the speech model
   (about 840 MB) and shows progress; after that it starts in a few seconds.
   Wait for: "ready … hold Right Ctrl, talk, release".

3. Open Notepad and click into it.
   Hold the RIGHT Ctrl key, say a sentence, let go.
   A small pill at the bottom of the screen shows Recording → Transcribing →
   Pasted, and the text appears in Notepad.

   Also try:
   - Right Ctrl + C (a shortcut) — it should NOT paste anything.
   - Copy some text first, dictate, then paste with Ctrl+V a few seconds
     later — your original copied text should come back.
   - Clicking the pill — Notepad should keep the cursor.

   To try a faster engine: close the console, then open a Command Prompt in
   this folder and run:   kaho-spike.exe --engine parakeet

4. Benchmark: in a Command Prompt in this folder, run
       kaho-spike.exe bench
   It downloads two more models (about 500 MB) and prints a table.
   Copy the whole table and send it back.

Logs: %LOCALAPPDATA%\KahoSpike\kaho-spike.log
Models: %LOCALAPPDATA%\KahoSpike\models (delete to free the disk space)

Known limits: unsigned; CPU only; no tray icon, settings or rewrite yet;
apps running as administrator will refuse the paste.
