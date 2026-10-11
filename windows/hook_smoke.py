"""CI smoke test: can a WH_KEYBOARD_LL hook be installed on this machine?

A hosted runner has no user at the keyboard, so this only proves the hook
installs and its message pump runs; real key-down/key-up timing needs the
owner's PC. Run from the windows/ folder.
"""

import sys
import time

from kaho_spike.hotkey import HotkeyHook

hook = HotkeyHook(lambda action: print("action", action))
hook.start()
if not hook.ready.wait(5):
    sys.exit("hook thread never reported in")
if hook.error:
    sys.exit(f"hook failed to install: {hook.error}")
time.sleep(1)
print("low-level keyboard hook installed and pumping")
