#!/usr/bin/env python3
"""List the GitHub repos of the Open Audio Stack registry that are not here yet.

  python3 scripts/import_oas.py > oas-repos.txt
  python3 scripts/add.py --from-file oas-repos.txt --jobs 6 --report oas-report.jsonl

The Open Audio Stack registry (https://github.com/open-audio-stack/open-audio-stack-registry)
lists open-source plugins with their release files. This script only picks the repos:
add.py then checks each one the usual way (license, stable release, VST3 archive, the
bundles themselves), so nothing from the other registry is trusted as is.

A repo is left out, with the reason on stderr, when
  * a manifest here already lists it (as `repo` or `releases`),
  * CANDIDATES.md already has it (reviewed and left out; --recheck to try again),
  * its binaries live in another repo (add those by hand with `releases`),
  * none of its files contain a VST3 (--any-format to try anyway).
Several Open Audio Stack entries of one repo come out as one line. A name that matches a
plugin already listed here is flagged in the line's comment; add.py skips it as a
duplicate if the scanned VST3 name really is the same.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry as reg  # noqa: E402

INDEX = "https://open-audio-stack.github.io/open-audio-stack-registry/index.json"
GITHUB_RE = re.compile(r"^https?://(www\.)?github\.com/[^/]+/[^/#?]+", re.I)


def load_index(src: str) -> dict:
    if re.match(r"^https?://", src):
        req = urllib.request.Request(src, headers={"User-Agent": reg.UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    return json.loads(Path(src).read_text(encoding="utf-8"))


def reviewed() -> set[str]:
    """Repos CANDIDATES.md already lists as checked and left out."""
    text = (reg.ROOT / "CANDIDATES.md").read_text(encoding="utf-8")
    return {reg.repo_key(m) for m in re.findall(r"\]\((https://github\.com/[^)]+)\)", text)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", default=INDEX, help="Open Audio Stack index.json, a URL or a file")
    ap.add_argument("--recheck", action="store_true", help="include repos CANDIDATES.md lists")
    ap.add_argument("--any-format", action="store_true", help="include entries with no VST3 file")
    args = ap.parse_args()

    by_repo, by_name = reg.owners()
    names = {n: pid for (fmt, n), pid in by_name.items()}
    seen_out = reviewed()

    # repo key -> Open Audio Stack entries of that repo
    groups: dict[str, list[tuple[str, dict]]] = {}
    left_out: list[tuple[str, str]] = []
    for slug, p in sorted(index_plugins(load_index(args.index)).items()):
        cur = p["versions"].get(p.get("version"), {})
        url = cur.get("url", "")
        if not GITHUB_RE.match(url):
            left_out.append((slug, f"source is not on GitHub ({url or 'no url'})"))
            continue
        groups.setdefault(reg.repo_key(url), []).append((slug, cur))

    out = []
    for repo, entries in sorted(groups.items()):
        slugs = ", ".join(s for s, _ in entries)
        files = [f for _, cur in entries for f in cur.get("files", [])]
        bin_repos = {reg.repo_key(f["url"]) for f in files if GITHUB_RE.match(f.get("url", ""))}
        if repo in by_repo or bin_repos & by_repo.keys():
            hit = by_repo.get(repo) or by_repo[next(iter(bin_repos & by_repo.keys()))]
            left_out.append((slugs, f"listed as {hit}"))
        elif repo in seen_out and not args.recheck:
            left_out.append((slugs, "reviewed in CANDIDATES.md"))
        elif bin_repos and repo not in bin_repos:
            left_out.append((slugs, f"binaries in {', '.join(sorted(bin_repos))}; add by hand with `releases`"))
        elif not args.any_format and not any("vst3" in (f.get("contains") or []) for f in files):
            left_out.append((slugs, "no VST3 file"))
        else:
            notes = [f"same name as {names[cur['name'].casefold()]}"
                     for _, cur in entries if cur.get("name", "").casefold() in names]
            out.append(f"{repo:<50} # oas: {slugs}" + (f"; {'; '.join(notes)}" if notes else ""))

    for slugs, why in left_out:
        print(f"skip {slugs}: {why}", file=sys.stderr)
    print(f"{len(out)} repo(s) to check, {len(left_out)} left out", file=sys.stderr)
    print("\n".join(out))
    return 0


def index_plugins(index: dict) -> dict:
    plugins = index.get("plugins")
    if not isinstance(plugins, dict):
        sys.exit("not an Open Audio Stack index: no 'plugins' object")
    return plugins


if __name__ == "__main__":
    sys.exit(main())
