#!/usr/bin/env python3
"""Fail when the illustrations or the version fall behind the code.

The README prose has been updated by hand three times while assets/menu.svg
still advertised a renamed rewrite mode or a menu item that no longer
existed. Images are the first thing a reader sees, and nothing else in CI
looks at them.

The version is checked for the same reason: it was written out in three
places by hand, and packaging/Sotto.spec sat at 1.7.3 through six releases.
install.sh and Sotto.spec now read sotto.py, so all that is left to verify is
that the changelog was actually written for the version being shipped.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "sotto.py").read_text()
MENU_SVG = (ROOT / "assets" / "menu.svg").read_text()
CHANGELOG = (ROOT / "CHANGELOG.md").read_text()


def app_version():
    m = re.search(r'^APP_VERSION = "([^"]+)"', SOURCE, re.MULTILINE)
    if not m:
        sys.exit("could not find APP_VERSION in sotto.py")
    return m.group(1)


def changelog_version():
    m = re.search(r"^## (\S+)", CHANGELOG, re.MULTILINE)
    if not m:
        sys.exit("could not find a version heading in CHANGELOG.md")
    return m.group(1)


def rewrite_modes():
    block = re.search(r"^REWRITE_MODES = \{(.*?)^\}", SOURCE, re.DOTALL | re.MULTILINE)
    if not block:
        sys.exit("could not find REWRITE_MODES in sotto.py")
    return re.findall(r'"[a-z]+":\s*"([^"]+)"', block.group(1))


def menu_items():
    """Titles from the status menu's `actions` tuple."""
    block = re.search(r"^        actions = \((.*?)^        \)", SOURCE, re.DOTALL | re.MULTILINE)
    if not block:
        sys.exit("could not find the status menu actions tuple in sotto.py")
    return re.findall(r'\("([^"]+)",\s*"[a-zA-Z]+:"', block.group(1))


def main():
    if app_version() != changelog_version():
        print(
            f"version mismatch: sotto.py says {app_version()}, the top of "
            f"CHANGELOG.md says {changelog_version()}.\n"
            "Add the changelog entry for this version, or correct APP_VERSION."
        )
        return 1
    missing = []
    for label in rewrite_modes():
        if f">{label}<" not in MENU_SVG:
            missing.append(f"rewrite mode {label!r}")
    for title in menu_items():
        if title not in MENU_SVG:
            missing.append(f"menu item {title!r}")
    if missing:
        print("assets/menu.svg is out of date with sotto.py:")
        for item in missing:
            print(f"  - {item} is in the code but not the illustration")
        print("\nUpdate assets/menu.svg so the README screenshots match the app.")
        return 1
    print(f"menu.svg matches sotto.py ({len(menu_items())} items, "
          f"{len(rewrite_modes())} rewrite modes), version {app_version()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
