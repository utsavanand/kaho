"""Render the stats branch README from daily download snapshots.

usage: python download_stats.py <stats dir> <releases.csv> <day>

<stats dir>/downloads.csv  day,tag,asset,downloads  one snapshot per asset per day
<stats dir>/internal.csv   day,asset,downloads,note  our own downloads (optional)
releases.csv               tag,asset,published,downloads  live counts from the API

GitHub only keeps a running total per asset, and replacing an asset resets it
to zero. The 2.4.0 zip lost three downloads that way. Lifetime counts are
rebuilt from the snapshots: a count that drops means the file was replaced,
and downloads before the drop still count.
"""

import csv
import sys
from collections import defaultdict


def lifetime_and_daily(rows):
    """Per-asset lifetime totals, and new downloads per (day, asset).

    The first snapshot of an asset counts everything before it as that day's.
    """
    by_asset = defaultdict(list)
    for r in rows:
        by_asset[(r["tag"], r["asset"])].append((r["day"], int(r["downloads"])))
    lifetime, daily, resets = {}, defaultdict(int), []
    for key, points in by_asset.items():
        total = previous = 0
        seen = set()
        for day, count in sorted(points):
            if day in seen:  # a snapshot recorded twice
                continue
            seen.add(day)
            if count < previous:
                resets.append((day, key[1], previous, count))
                new = count
            else:
                new = count - previous
            total += new
            if new:
                daily[(day, key)] += new
            previous = count
        lifetime[key] = total
    return lifetime, daily, resets


def render(snapshots, internal, releases, day):
    lifetime, daily, resets = lifetime_and_daily(snapshots)
    # Today's live counts can be ahead of the snapshot: take whichever is larger
    for tag, asset, _, count in releases:
        key = (tag, asset)
        last = max((int(r["downloads"]) for r in snapshots
                    if (r["tag"], r["asset"]) == key and r["day"] == day), default=None)
        if last is None:
            lifetime[key] = lifetime.get(key, 0) + int(count)
        elif int(count) > last:
            lifetime[key] += int(count) - last
    ours = defaultdict(int)
    for r in internal:
        ours[r["asset"]] += int(r["downloads"])
    published = {(t, a): p for t, a, p, _ in releases}

    total = sum(lifetime.values())
    our_total = sum(min(ours[a], n) for (_, a), n in lifetime.items())
    lines = [
        "# Kaho downloads", "",
        (f"**Outside downloads: {total - our_total:,}** of {total:,} in total "
         f"(all releases and files, updated {day})."), "",
        ("Every download of a release file counts, including Homebrew installs and the "
         "website's Download button. **Ours** are downloads by the maintainers' own "
         "release checks, listed in [stats/internal.csv](stats/internal.csv); they are "
         "recorded from 2.4.0 on, so earlier releases show none. From 2.4.2, releases are "
         "verified against GitHub's published checksums instead of being downloaded."), "",
        "## By file", "",
        "| Release | File | Published | Downloads | Ours | Outside |",
        "|---|---|---|---:|---:|---:|",
    ]
    for (tag, asset), n in sorted(lifetime.items(), key=lambda kv: published.get(kv[0], ""),
                                  reverse=True):
        o = min(ours[asset], n)
        lines.append(f"| {tag} | {asset} | {published.get((tag, asset), '')} | {n:,} | {o:,} | {n - o:,} |")
    lines += ["", "## New downloads by day (last 60 days)", "",
              "Snapshots run daily at 06:15 UTC, so a download counts toward the next snapshot.", "",
              "| Day | File | New |", "|---|---|---:|"]
    days = sorted({d for d, _ in daily}, reverse=True)[:60]
    first = min((r["day"] for r in snapshots), default=None)
    for d in days:
        # The first snapshot holds everything downloaded before tracking began
        label = f"up to {d}" if d == first else d
        for (dd, (tag, asset)), n in sorted(daily.items()):
            if dd == d:
                lines.append(f"| {label} | {asset} | {n:,} |")
    if resets:
        lines += ["", "## Replaced files", "",
                  "Replacing a release file resets GitHub's count. The downloads before are kept above.", "",
                  "| Day | File | Count before | Count after |", "|---|---|---:|---:|"]
        lines += [f"| {d} | {a} | {b:,} | {c:,} |" for d, a, b, c in sorted(resets)]
    lines += ["", ("Raw data: [stats/downloads.csv](stats/downloads.csv). Updated daily by "
                   "`.github/workflows/download-stats.yml` on main.")]
    return "\n".join(lines) + "\n"


def read_csv(path, reader=csv.DictReader):
    with open(path, newline="") as f:
        return list(reader(f))


def main():
    stats, releases_path, day = sys.argv[1:4]
    snapshots = read_csv(f"{stats}/downloads.csv")
    try:
        internal = read_csv(f"{stats}/internal.csv")
    except FileNotFoundError:
        internal = []
    releases = read_csv(releases_path, csv.reader)
    sys.stdout.write(render(snapshots, internal, releases, day))


if __name__ == "__main__":
    main()
