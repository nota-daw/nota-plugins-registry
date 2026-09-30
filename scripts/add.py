#!/usr/bin/env python3
"""Draft (or update) registry manifests straight from GitHub releases.

  python3 scripts/add.py owner/repo [owner/repo ...] [--tag TAG] [--jobs N] [--max-size MB]
  python3 scripts/add.py --from-file repos.txt --report report.jsonl

For each repo: take the latest stable release (or --tag), pick the best archive per
OS from the asset names, download + sha256 + unpack it (following nested .pkg/.dmg/.zip
when the outer archive has no bundle), then read the VST3 bundles themselves:

  * platform key from the binaries, not the file name: Mach-O slices (universal /
    arm64 / x64), Contents/<arch>-win|linux folders, PE machine of a single-file .vst3
  * plugin names + instrument/effect from Contents/Resources/moduleinfo.json, or by
    loading the bundle in Nota's scan worker (--worker, macOS) when there is none

and writes plugins/<id>.json — a new manifest, or a new version prepended to an
existing one. Descriptions come from the repo and deserve a human pass before merging.
Prints one JSON line per repo (status: added | updated | same | skipped | failed).
Needs a GitHub token: GITHUB_TOKEN, or a logged-in `gh`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry as reg  # noqa: E402

WORKER_DEFAULT = os.environ.get("NOTA_SCANWORKER", "")
_write_lock = threading.Lock()


# --- GitHub -----------------------------------------------------------------------

def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN", "")
    if not tok and shutil.which("gh"):
        tok = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
    if not tok:
        sys.exit("add.py needs a GitHub token: set GITHUB_TOKEN or log in with `gh auth login`")
    return tok


TOKEN = ""


def gh(path: str):
    req = urllib.request.Request("https://api.github.com/" + path, headers={
        "Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json", "User-Agent": reg.UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


UNSTABLE = re.compile(r"nightly|latest|continuous|snapshot|dev|beta|alpha|(?<![a-z])rc[.\-_]?\d*|preview|pre[.\-_]?\d", re.I)


def pick_release(repo: str, tag: str | None) -> dict:
    if tag:
        return gh(f"repos/{repo}/releases/tags/{tag}")
    for rel in gh(f"repos/{repo}/releases?per_page=20"):
        if not rel["draft"] and not rel["prerelease"] and not UNSTABLE.search(rel["tag_name"]) and rel["assets"]:
            return rel
    raise Skip("no stable release with assets")


def release_version(rel: dict) -> str:
    """"v1.2.3", "Foo-1.2.3", "Foo@1.2.3" -> "1.2.3"; else the release name's version; else its date."""
    for text in (rel["tag_name"], rel.get("name") or ""):
        m = re.search(r"\d+(?:\.\d+)+(?:[-+.]?[A-Za-z0-9]+)*", text)
        if m:
            return m.group(0)
    return rel["published_at"][:10].replace("-", ".")


class Skip(Exception):
    pass


# --- asset choice -----------------------------------------------------------------

def asset_os(name: str) -> str | None:
    n = name.lower()
    kind = reg.archive_kind_of(n)
    if re.search(r"mac|osx|darwin|apple", n) or kind in ("dmg", "pkg"):
        return "macos"
    if re.search(r"win", n):
        return "windows"
    if re.search(r"linux|lnx|ubuntu|debian", n) or kind == "deb":
        return "linux"
    return None


def asset_rank(name: str) -> int | None:
    """Higher is better; None = never (sources, 32-bit, other formats only)."""
    n = name.lower()
    if reg.archive_kind_of(n) is None or re.search(r"src|source|debug|symbols|pdb", n):
        return None
    if re.search(r"i686|i386|win32|x86(?!_64)|32.?bit|armhf|armv7|riscv", n):
        return None
    score = 0
    if "vst3" in n:
        score += 5
    for other in ("clap", "lv2", "aax", "standalone", "vst2", "ladspa", "dssi", "app"):
        if re.search(rf"(^|[^a-z]){other}([^a-z]|$)", n) and "vst3" not in n:
            score -= 4
    if re.search(r"(^|[^a-z])au([^a-z]|$)", n) and "vst3" not in n:
        score -= 4
    if reg.archive_kind_of(n) == "deb":
        score -= 1  # prefer a plain archive when both exist
    return score


def asset_arch_hint(name: str) -> str:
    n = name.lower()
    if re.search(r"arm64|aarch64", n):
        return "arm64"
    if re.search(r"x64|x86_64|amd64|win64|intel", n):
        return "x64"
    return "any"


# --- reading bundles --------------------------------------------------------------

MACHO = {0x01000007: "x64", 0x0100000C: "arm64"}
PE = {0x8664: "x64", 0xAA64: "arm64"}


def macho_archs(path: Path) -> set[str]:
    try:
        with path.open("rb") as f:
            head = f.read(8)
            magic = struct.unpack(">I", head[:4])[0]
            if magic in (0xCAFEBABE, 0xCAFEBABF):  # fat (big-endian header)
                n = struct.unpack(">I", head[4:8])[0]
                size = 20 if magic == 0xCAFEBABE else 32
                archs = set()
                for i in range(n):
                    f.seek(8 + i * size)
                    cpu = struct.unpack(">I", f.read(4))[0]
                    if cpu in MACHO:
                        archs.add(MACHO[cpu])
                return archs
            if struct.unpack("<I", head[:4])[0] == 0xFEEDFACF:
                return {MACHO.get(struct.unpack("<I", head[4:8])[0], "?")}
    except OSError:
        pass
    return set()


def pe_arch(path: Path) -> set[str]:
    try:
        with path.open("rb") as f:
            if f.read(2) != b"MZ":
                return set()
            f.seek(0x3C)
            off = struct.unpack("<I", f.read(4))[0]
            f.seek(off)
            if f.read(4) != b"PE\0\0":
                return set()
            machine = struct.unpack("<H", f.read(2))[0]
            return {PE[machine]} if machine in PE else {"?"}
    except OSError:
        return set()


def bundle_archs(bundle: Path) -> dict[str, set[str]]:
    """{os: archs} the bundle carries binaries for."""
    out: dict[str, set[str]] = {}
    if bundle.is_file():  # legacy single-file Windows .vst3 (a DLL)
        a = pe_arch(bundle)
        return {"windows": a} if a else {}
    contents = bundle / "Contents"
    macos = contents / "MacOS"
    if macos.is_dir():
        archs: set[str] = set()
        for exe in macos.iterdir():
            if exe.is_file():
                archs |= macho_archs(exe)
        if archs:
            out["macos"] = archs
    if contents.is_dir():
        for d in contents.iterdir():
            m = re.match(r"^(x86_64|x64|arm64|arm64ec|arm64x|aarch64)-(win|linux)$", d.name)
            if m and d.is_dir():
                arch = "x64" if m.group(1) in ("x86_64", "x64") else "arm64"
                out.setdefault("windows" if m.group(2) == "win" else "linux", set()).add(arch)
                if m.group(1) == "arm64x":
                    out["windows"].add("x64")
    return out


def platform_keys(os_: str, archs: set[str]) -> list[str]:
    archs = archs - {"?"}
    if os_ == "macos":
        if {"x64", "arm64"} <= archs:
            return ["macos-universal"]
        return [f"macos-{a}" for a in sorted(archs)]
    return [f"{os_}-{a}" for a in sorted(archs)]


def lenient_json(text: str):
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return json.loads(text)


def moduleinfo(bundle: Path) -> list[tuple[str, bool]]:
    """(name, is_instrument) for each audio class in the bundle's moduleinfo.json."""
    info = bundle / "Contents" / "Resources" / "moduleinfo.json"
    if not info.is_file():
        return []
    try:
        data = lenient_json(info.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        return []
    out = []
    for c in data.get("Classes", []):
        if c.get("Category") != "Audio Module Class":
            continue
        subs = c.get("Sub Categories") or []
        if isinstance(subs, str):
            subs = subs.split("|")
        out.append((c.get("Name", "").strip(), any(s.lower().startswith("instrument") for s in subs)))
    return [o for o in out if o[0]]


def scan_worker(bundle: Path, worker: str) -> list[tuple[str, bool]]:
    try:
        out = subprocess.run([worker, "VST3", str(bundle)], capture_output=True, text=True, timeout=120).stdout
    except (subprocess.TimeoutExpired, OSError):
        return []
    return [(n, i == "1") for n, i in re.findall(r'<PLUGIN name="([^"]+)".*?isInstrument="(\d)"', out, re.S)]


def host_arch() -> str:
    import platform
    return "arm64" if platform.machine() in ("arm64", "aarch64") else "x64"


# --- one asset --------------------------------------------------------------------

def dedupe_bundles(paths: list[str]) -> list[str]:
    """One path per bundle name, preferring 64-bit-looking and shallow paths."""
    best: dict[str, str] = {}
    for p in paths:
        name = p.rsplit("/", 1)[-1]
        def score(q: str) -> tuple:
            bad = bool(re.search(r"32|x86(?!_64)|i386|win32", q, re.I))
            return (bad, q.count("/"), len(q))
        if name not in best or score(p) < score(best[name]):
            best[name] = p
    return sorted(best.values())


def examine(asset: dict, work: Path, worker: str) -> dict:
    """Download + unpack an asset; describe its bundles."""
    path = reg.download(asset["browser_download_url"])
    kind = reg.archive_kind_of(asset["name"])
    root = work / "outer"
    reg.unpack(path, kind, root)
    inner = None
    bundles = reg.find_bundles(root)
    if not bundles:
        for nested in reg.nested_archives(root):
            nroot = work / ("inner-" + str(abs(hash(nested))))
            try:
                reg.unpack(root / nested, reg.archive_kind_of(nested), nroot)
            except Exception:  # noqa: BLE001
                continue
            found = reg.find_bundles(nroot)
            if found:
                inner, root, bundles = nested, nroot, found
                break
    if not bundles:
        raise Skip(f"no .vst3 in {asset['name']}")
    bundles = dedupe_bundles(bundles)

    per_os: dict[str, set[str]] = {}
    provides: list[tuple[str, bool]] = []
    for b in bundles:
        archs_here = bundle_archs(root / b)
        for os_, archs in archs_here.items():
            # Every bundle must run on the platform key: intersect across bundles.
            per_os[os_] = per_os[os_] & archs if os_ in per_os else set(archs)
        names = moduleinfo(root / b)
        if not names and worker and sys.platform == "darwin" and host_arch() in archs_here.get("macos", set()):
            names = scan_worker(root / b, worker)
        provides += names or [(Path(b).stem, False)]
    return {
        "url": asset["browser_download_url"], "sha256": reg.sha256(path), "size": path.stat().st_size,
        "inner": inner, "bundles": bundles, "per_os": per_os, "provides": provides,
    }


# --- one repo ---------------------------------------------------------------------

# Topics that describe formats, platforms or tooling rather than the sound.
NOISE_TOPICS = {
    "vst3", "vst", "vst2", "juce", "juce-framework", "juce-plugin", "audio-plugin", "audio-plugins", "plugin",
    "plugins", "clap", "clap-plugin", "au", "audio-unit", "audiounit", "lv2", "lv2-plugin", "vst3-plugin",
    "vst-plugin", "audio", "linux", "macos", "windows", "mac", "osx", "cpp", "c-plus-plus", "cmake", "dpf",
    "rust", "nih-plug", "iplug2", "daw", "music", "audio-processing", "dsp", "open-source", "free",
}


def existing_id(repo_url: str) -> str | None:
    """The id of the manifest already listing this repo, if any."""
    for p in reg.PLUGINS.glob("*.json"):
        m = json.loads(p.read_text(encoding="utf-8"))
        if m.get("repo", "").lower().rstrip("/") == repo_url.lower().rstrip("/"):
            return m["id"]
    return None


def free_slug(meta: dict) -> str:
    """repo-name, or owner-repo-name when another plugin already took that id."""
    for cand in (slug(meta["name"]), slug(f"{meta['owner']['login']}-{meta['name']}")):
        if not (reg.PLUGINS / f"{cand}.json").exists():
            return cand
    raise Skip(f"id {slug(meta['name'])} is taken")


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:50] or "plugin"


def add_repo(repo: str, tag: str | None, worker: str, max_size: int, id_override: str | None) -> dict:
    meta = gh(f"repos/{repo}")
    repo = meta["full_name"]
    lic = (meta.get("license") or {}).get("spdx_id")
    if lic not in reg.LICENSES:
        raise Skip(f"license {lic}")
    note_archived = bool(meta.get("archived"))
    rel = pick_release(repo, tag)
    version = release_version(rel)

    # Candidate assets per OS, best first.
    by_os: dict[str, list[dict]] = {}
    too_big = []
    for a in rel["assets"]:
        os_ = asset_os(a["name"])
        rank = asset_rank(a["name"])
        if not os_ or rank is None:
            continue
        if a["size"] > max_size:
            too_big.append(a["name"])
            continue
        by_os.setdefault(os_, []).append({**a, "rank": rank, "hint": asset_arch_hint(a["name"])})
    if not by_os:
        raise Skip("no usable archive" + (f" (over size cap: {', '.join(too_big)})" if too_big else ""))

    assets: dict[str, dict] = {}
    provides: dict[str, bool] = {}
    notes = []
    for os_, cands in by_os.items():
        cands.sort(key=lambda c: (-c["rank"], c["size"]))
        # Per-arch files (x64 + arm64 zips) are separate candidates; try each hint group.
        tried_hints = set()
        for c in cands:
            if c["hint"] in tried_hints and c["hint"] != "any":
                continue
            with tempfile.TemporaryDirectory(prefix="nota-add-") as tmp:
                try:
                    ex = examine(c, Path(tmp), worker)
                except Skip:
                    continue
                except Exception as e:  # noqa: BLE001
                    notes.append(f"{c['name']}: {e}")
                    continue
            keys = platform_keys(os_, ex["per_os"].get(os_, set()))
            if not keys:
                notes.append(f"{c['name']}: no {os_} binaries inside")
                continue
            tried_hints.add(c["hint"])
            entry = {"url": ex["url"], "sha256": ex["sha256"], "size": ex["size"]}
            if ex["inner"]:
                entry["inner"] = ex["inner"]
            entry["bundles"] = ex["bundles"]
            for k in keys:
                assets.setdefault(k, entry)
            for n, inst in ex["provides"]:
                provides[n] = provides.get(n, False) or inst
            if c["hint"] == "any":
                break
    if not assets:
        raise Skip("no archive with VST3 bundles" + (f" ({'; '.join(notes)})" if notes else ""))
    # A universal build covers both Mac archs; drop the redundant single-arch entries.
    if "macos-universal" in assets:
        for k in ("macos-arm64", "macos-x64"):
            if k in assets and assets[k]["url"] == assets["macos-universal"]["url"]:
                del assets[k]

    names = sorted(provides)
    instruments = [n for n in names if provides[n]]
    kind = "bundle" if len(names) >= 4 else ("instrument" if instruments else "effect")
    pid = id_override or existing_id(meta["html_url"]) or free_slug(meta)
    path = reg.PLUGINS / f"{pid}.json"
    status = "added"
    with _write_lock:
        if path.exists():
            m = json.loads(path.read_text(encoding="utf-8"))
            if m["versions"][0]["version"] == version:
                return {"repo": repo, "id": pid, "status": "same", "version": version}
            m["versions"].insert(0, {"version": version, "assets": dict(sorted(assets.items()))})
            known = {p["name"] for p in m["provides"]}
            m["provides"] += [{"format": "VST3", "name": n} for n in names if n not in known]
            status = "updated"
        else:
            owner = gh(f"users/{meta['owner']['login']}")
            desc = (meta.get("description") or "").strip()
            if len(desc) > 200:
                desc = desc[:197].rsplit(" ", 1)[0] + "…"
            m = {
                "id": pid,
                "name": names[0] if len(names) == 1 else meta["name"],
                "developer": owner.get("name") or owner["login"],
                "description": desc or f"{meta['name']} audio plugin.",
            }
            if (meta.get("homepage") or "").startswith("https://"):
                m["homepage"] = meta["homepage"]
            m.update({
                "repo": meta["html_url"],
                "license": lic,
                "kind": kind,
                "tags": [t for t in meta.get("topics", []) if t not in NOISE_TOPICS][:6],
                "provides": [{"format": "VST3", "name": n} for n in names],
                "versions": [{"version": version, "assets": dict(sorted(assets.items()))}],
            })
            if note_archived:
                m["notes"] = "The project is archived upstream; this is its last release."
        errs = reg.validate_manifest(path, m)
        if errs:
            raise Skip("manifest invalid: " + "; ".join(errs))
        path.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"repo": repo, "id": pid, "status": status, "version": version, "kind": kind,
            "platforms": sorted(assets), "provides": names, "notes": notes, "too_big": too_big}


def main() -> int:
    global TOKEN
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repos", nargs="*")
    ap.add_argument("--from-file")
    ap.add_argument("--tag")
    ap.add_argument("--id", help="manifest id (single repo only)")
    ap.add_argument("--worker", default=WORKER_DEFAULT, help="nota-scanworker, used when a bundle has no moduleinfo.json")
    ap.add_argument("--max-size", type=int, default=300, help="skip assets larger than this many MB")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--report", help="append JSON lines here as well")
    args = ap.parse_args()
    TOKEN = _token()
    repos = list(args.repos)
    if args.from_file:
        repos += [l.split("#")[0].strip() for l in open(args.from_file) if l.split("#")[0].strip()]
    if args.id and len(repos) != 1:
        sys.exit("--id needs exactly one repo")

    def one(repo: str) -> dict:
        try:
            r = add_repo(repo, args.tag, args.worker, args.max_size << 20, args.id)
        except Skip as e:
            r = {"repo": repo, "status": "skipped", "reason": str(e)}
        except urllib.error.HTTPError as e:
            r = {"repo": repo, "status": "failed", "reason": f"GitHub {e.code}"}
        except Exception as e:  # noqa: BLE001
            r = {"repo": repo, "status": "failed", "reason": f"{type(e).__name__}: {e}"}
        line = json.dumps(r, ensure_ascii=False)
        with _write_lock:
            print(line, flush=True)
            if args.report:
                with open(args.report, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        return r

    with ThreadPoolExecutor(args.jobs) as ex:
        results = list(ex.map(one, repos))
    bad = [r for r in results if r["status"] == "failed"]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
