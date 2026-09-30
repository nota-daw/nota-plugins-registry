#!/usr/bin/env python3
"""Nota plugin registry tool (stdlib only).

  validate            static checks on every manifest in plugins/
  build [--out DIR]   write DIR/index.json (what Nota downloads)
  verify [ID ...]     download assets, check size + sha256, unpack, check bundles exist
  inspect URL|FILE    download + hash + unpack one release asset and list its .vst3 bundles
                      (the quickest way to fill in a new manifest version)
  outdated            compare each plugin's newest version with its latest GitHub release

verify/inspect unpack .dmg and .pkg with hdiutil / pkgutil, so they need macOS for
those assets; zip, tar.* and .deb work anywhere.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"
CACHE = Path(os.environ.get("NOTA_REGISTRY_CACHE", ROOT / ".cache"))

SCHEMA_VERSION = 1
PLATFORMS = {
    "macos-universal", "macos-arm64", "macos-x64",
    "windows-x64", "windows-arm64",
    "linux-x64", "linux-arm64",
}
KINDS = {"instrument", "effect", "midi", "bundle"}
ARCHIVES = {"zip", "tar", "dmg", "pkg", "deb"}
# OSI-approved licenses only: the registry lists open-source plugins. The bare
# "GPL-3.0"-style ids are what GitHub's license detection reports for a repo.
LICENSES = {
    "GPL-2.0", "GPL-3.0", "LGPL-2.1", "LGPL-3.0", "AGPL-3.0",
    "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only", "GPL-3.0-or-later",
    "LGPL-2.1-or-later", "LGPL-3.0-or-later", "AGPL-3.0-only", "AGPL-3.0-or-later",
    "MIT", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "ISC", "Apache-2.0", "MPL-2.0", "Zlib",
    "BSL-1.0", "Unlicense",
}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
URL_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/releases/download/[^/]+/[^/]+$")
UA = "nota-plugins-registry"


# --- manifests ------------------------------------------------------------------

def load_manifests() -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(PLUGINS.glob("*.json")):
        with p.open(encoding="utf-8") as f:
            out.append((p, json.load(f)))
    return out


def archive_kind(asset: dict) -> str | None:
    if "archive" in asset:
        return asset["archive"]
    return archive_kind_of(asset.get("url", ""))


def archive_kind_of(name: str) -> str | None:
    n = name.lower()
    if n.endswith(".zip"):
        return "zip"
    if re.search(r"\.(tar|tar\.gz|tgz|tar\.xz|txz|tar\.bz2)$", n):
        return "tar"
    for ext in ("dmg", "pkg", "deb"):
        if n.endswith("." + ext):
            return ext
    return None


def validate_manifest(path: Path, m: dict) -> list[str]:
    errs: list[str] = []

    def err(msg: str) -> None:
        errs.append(f"{path.name}: {msg}")

    for key in ("id", "name", "developer", "description", "repo", "license", "kind", "provides", "versions"):
        if key not in m:
            err(f"missing '{key}'")
    if errs:
        return errs

    if not ID_RE.match(m["id"]):
        err(f"id '{m['id']}' must be lowercase letters, digits and dashes")
    if path.stem != m["id"]:
        err(f"file name must be '{m['id']}.json'")
    if not m["repo"].startswith("https://github.com/"):
        err("repo must be a https://github.com/ URL")
    if m["license"] not in LICENSES:
        err(f"license '{m['license']}' is not in the allowed SPDX list")
    if m["kind"] not in KINDS:
        err(f"kind must be one of {sorted(KINDS)}")
    if len(m["description"]) > 200:
        err("description must be at most 200 characters")
    if "homepage" in m and not m["homepage"].startswith("https://"):
        err("homepage must be an https URL")
    if "releases" in m and not re.match(r"^[\w.-]+/[\w.-]+$", m["releases"]):
        err("releases must be 'owner/repo' (the repo that publishes the binaries)")
    if not isinstance(m.get("tags", []), list) or not all(isinstance(t, str) for t in m.get("tags", [])):
        err("tags must be a list of strings")
    if len(m.get("notes", "")) > 200:
        err("notes must be at most 200 characters")
    unknown = set(m) - {"id", "name", "developer", "description", "homepage", "repo", "releases",
                        "license", "kind", "tags", "notes", "provides", "versions"}
    if unknown:
        err(f"unknown field(s) {sorted(unknown)}")

    provides = m["provides"]
    if not isinstance(provides, list) or not provides:
        err("provides must be a non-empty list")
    else:
        for i, pr in enumerate(provides):
            if pr.get("format") != "VST3" or not pr.get("name"):
                err(f"provides[{i}] needs format 'VST3' and the plugin's display name")

    versions = m["versions"]
    if not isinstance(versions, list) or not versions:
        err("versions must be a non-empty list, newest first")
        return errs
    seen = set()
    for v in versions:
        ver = v.get("version")
        if not ver or ver in seen:
            err(f"version '{ver}' is missing or duplicated")
        seen.add(ver)
        assets = v.get("assets") or {}
        if not assets:
            err(f"{ver}: no assets")
        for plat, a in assets.items():
            where = f"{ver}/{plat}"
            if plat not in PLATFORMS:
                err(f"{where}: unknown platform (use one of {sorted(PLATFORMS)})")
            if not URL_RE.match(a.get("url", "")):
                err(f"{where}: url must be a GitHub release download URL")
            if not SHA_RE.match(a.get("sha256", "")):
                err(f"{where}: sha256 must be 64 lowercase hex chars")
            if not isinstance(a.get("size"), int) or a["size"] <= 0:
                err(f"{where}: size must be the asset's byte count")
            kind = archive_kind(a)
            if kind not in ARCHIVES:
                err(f"{where}: can't tell the archive type; set 'archive' to one of {sorted(ARCHIVES)}")
            if kind == "pkg" and not plat.startswith("macos"):
                err(f"{where}: .pkg assets are macOS-only")
            if kind == "dmg" and not plat.startswith("macos"):
                err(f"{where}: .dmg assets are macOS-only")
            inner = a.get("inner")
            if inner is not None and (archive_kind_of(inner) not in ARCHIVES or _unsafe(inner)):
                err(f"{where}: inner must be a relative path to a nested archive")
            bundles = a.get("bundles")
            if not isinstance(bundles, list) or not bundles:
                err(f"{where}: bundles must list the .vst3 paths inside the archive")
            else:
                for b in bundles:
                    if not b.endswith(".vst3") or _unsafe(b):
                        err(f"{where}: bundle '{b}' must be a relative path ending in .vst3")
    return errs


def _unsafe(rel: str) -> bool:
    return rel.startswith(("/", "\\")) or ".." in Path(rel).parts or ":" in rel


def cmd_validate(_args) -> int:
    manifests = load_manifests()
    errs: list[str] = []
    ids = set()
    for path, m in manifests:
        errs += validate_manifest(path, m)
        if m.get("id") in ids:
            errs.append(f"{path.name}: duplicate id")
        ids.add(m.get("id"))
    for e in errs:
        print("error:", e)
    print(f"{len(manifests)} manifest(s), {len(errs)} error(s)")
    return 1 if errs else 0


def cmd_build(args) -> int:
    if cmd_validate(args):
        return 1
    plugins = [m for _, m in load_manifests()]
    plugins.sort(key=lambda m: m["name"].lower())
    index = {
        "schema": SCHEMA_VERSION,
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plugins": plugins,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.json").write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "index.html").write_text(_html(plugins), encoding="utf-8")
    print(f"wrote {out / 'index.json'} ({len(plugins)} plugins)")
    return 0


def _html(plugins: list[dict]) -> str:
    from html import escape
    rows = "\n".join(
        f"<tr><td><a href='{escape(m['repo'])}'>{escape(m['name'])}</a></td><td>{escape(m['developer'])}</td>"
        f"<td>{escape(m['kind'])}</td><td>{escape(m['versions'][0]['version'])}</td><td>{escape(m['license'])}</td>"
        f"<td>{escape(', '.join(sorted(m['versions'][0]['assets'])))}</td></tr>"
        for m in plugins)
    return ("<!doctype html><meta charset=utf-8><title>Nota plugin registry</title>"
            "<style>body{font:14px system-ui;margin:2em}td,th{padding:4px 10px;text-align:left}</style>"
            "<h1>Nota plugin registry</h1><p>Open-source plugins installable from Nota. "
            "Machine-readable: <a href='index.json'>index.json</a>.</p>"
            f"<table><tr><th>Plugin</th><th>Developer</th><th>Kind</th><th>Version</th><th>License</th><th>Platforms</th></tr>{rows}</table>\n")


# --- download + unpack ----------------------------------------------------------

def download(url: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    dest = CACHE / hashlib.sha1(url.encode()).hexdigest()[:12] / url.rsplit("/", 1)[-1]
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r, tmp.open("wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    tmp.rename(dest)
    return dest


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unpack(archive: Path, kind: str, dest: Path) -> None:
    """Expands archive into dest (which must not exist yet). Never runs installer scripts."""
    if kind == "zip":
        with zipfile.ZipFile(archive) as z:
            backslashed = any("\\" in n for n in z.namelist())
        if shutil.which("ditto") and not backslashed:  # keeps macOS bundle symlinks + permissions
            subprocess.run(["ditto", "-x", "-k", str(archive), str(dest)], check=True)
        else:
            _unzip(archive, dest)
    elif kind == "tar":
        with tarfile.open(archive) as t:
            _untar(t, dest)
    elif kind == "dmg":
        _need("hdiutil", kind)
        mnt = Path(tempfile.mkdtemp(prefix="nota-dmg-"))
        # "Y" answers a license agreement if the image carries one.
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen",
                        "-mountpoint", str(mnt), str(archive)],
                       input=b"Y\n", check=True, stdout=subprocess.DEVNULL)
        try:
            shutil.copytree(mnt, dest, symlinks=True, ignore=shutil.ignore_patterns(".Trashes", ".fseventsd"))
        finally:
            subprocess.run(["hdiutil", "detach", str(mnt), "-force"], check=False, stdout=subprocess.DEVNULL)
    elif kind == "pkg":
        _need("pkgutil", kind)
        # --expand-full unpacks payloads only; preinstall/postinstall scripts are not run.
        subprocess.run(["pkgutil", "--expand-full", str(archive), str(dest)], check=True)
    elif kind == "deb":
        _unpack_deb(archive, dest)
    else:
        raise ValueError(f"unsupported archive type {kind}")


def _unzip(archive: Path, dest: Path) -> None:
    # Some Windows-built zips store "a\\b\\c" paths; treat the backslash as a separator.
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            rel = info.filename.replace("\\", "/")
            if _unsafe(rel):
                raise ValueError(f"unsafe path in archive: {info.filename}")
            target = dest / rel
            if rel.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            mode = info.external_attr >> 16
            if mode & 0o111:
                target.chmod(0o755)


def _untar(t: tarfile.TarFile, dest: Path) -> None:
    if hasattr(tarfile, "data_filter"):  # Python 3.12+ (and security backports)
        t.extractall(dest, filter="data")
        return
    for member in t.getmembers():
        link = os.path.normpath(os.path.join(os.path.dirname(member.name), member.linkname)) \
            if member.issym() else member.linkname
        if _unsafe(member.name) or (member.issym() or member.islnk()) and _unsafe(link):
            raise ValueError(f"unsafe path in archive: {member.name}")
    t.extractall(dest)


def _need(tool: str, kind: str) -> None:
    if not shutil.which(tool):
        raise RuntimeError(f"unpacking .{kind} needs '{tool}' (macOS)")


def _unpack_deb(archive: Path, dest: Path) -> None:
    # A .deb is an ar archive; the files live in data.tar.{gz,xz,bz2}.
    with archive.open("rb") as f:
        if f.read(8) != b"!<arch>\n":
            raise ValueError("not a .deb (ar) archive")
        while header := f.read(60):
            name = header[:16].decode().strip().rstrip("/")
            size = int(header[48:58].decode().strip())
            data = f.read(size)
            if size % 2:
                f.read(1)
            if name.startswith("data.tar"):
                tmp = dest.parent / name
                tmp.write_bytes(data)
                try:
                    with tarfile.open(tmp) as t:
                        _untar(t, dest)
                finally:
                    tmp.unlink()
                return
    raise ValueError("no data.tar.* member in .deb")


def expand_asset(asset: dict, work: Path) -> Path:
    """Download + check + unpack an asset (and its inner archive). Returns the bundle root."""
    path = download(asset["url"])
    size = path.stat().st_size
    if size != asset["size"]:
        raise ValueError(f"size {size} != manifest {asset['size']}")
    digest = sha256(path)
    if digest != asset["sha256"]:
        raise ValueError(f"sha256 {digest} != manifest {asset['sha256']}")
    root = work / "outer"
    unpack(path, archive_kind(asset), root)
    if inner := asset.get("inner"):
        nested = root / inner
        if not nested.exists():
            raise ValueError(f"inner archive '{inner}' not found")
        root = work / "inner"
        unpack(nested, archive_kind_of(inner), root)
    return root


def find_bundles(root: Path) -> list[str]:
    """Outermost *.vst3 entries under root, as sorted relative paths."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames):
            if name.lower().endswith(".vst3"):
                found.append(os.path.relpath(os.path.join(dirpath, name), root))
                dirnames.remove(name)  # don't descend into a bundle
        for name in filenames:
            if name.lower().endswith(".vst3"):
                found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(p.replace(os.sep, "/") for p in found)


def nested_archives(root: Path) -> list[str]:
    out = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if archive_kind_of(name) in {"pkg", "zip", "dmg"}:
                out.append(os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/"))
    # pkgutil --expand-full leaves component packages as directories named *.pkg
    return sorted(out)


def cmd_verify(args) -> int:
    manifests = {m["id"]: m for _, m in load_manifests()}
    ids = args.ids or sorted(manifests)
    failures = 0
    for pid in ids:
        m = manifests.get(pid)
        if m is None:
            print(f"error: no plugin '{pid}'")
            failures += 1
            continue
        latest = m["versions"][0]
        for plat, asset in sorted(latest["assets"].items()):
            label = f"{pid} {latest['version']} {plat}"
            kind = archive_kind(asset)
            needs_mac = kind in ("dmg", "pkg") or archive_kind_of(asset.get("inner", "")) in ("dmg", "pkg")
            if needs_mac and sys.platform != "darwin":
                print(f"skip  {label} (needs macOS to unpack)")
                continue
            with tempfile.TemporaryDirectory(prefix="nota-verify-") as tmp:
                try:
                    root = expand_asset(asset, Path(tmp))
                    missing = [b for b in asset["bundles"] if not (root / b).exists()]
                    if missing:
                        raise ValueError(f"bundles not found: {missing}; archive has {find_bundles(root)}")
                    print(f"ok    {label}")
                except Exception as e:  # noqa: BLE001 — report and carry on
                    print(f"FAIL  {label}: {e}")
                    failures += 1
                finally:
                    # CI runners have little disk: don't keep hundreds of archives around.
                    if os.environ.get("CI"):
                        for f in CACHE.glob(f"*/{asset['url'].rsplit('/', 1)[-1]}"):
                            f.unlink(missing_ok=True)
    print(f"{failures} failure(s)")
    return 1 if failures else 0


def cmd_inspect(args) -> int:
    src = args.source
    path = Path(src) if Path(src).exists() else download(src)
    kind = args.archive or archive_kind_of(path.name)
    print(f"file    {path.name}")
    print(f"size    {path.stat().st_size}")
    print(f"sha256  {sha256(path)}")
    with tempfile.TemporaryDirectory(prefix="nota-inspect-") as tmp:
        root = Path(tmp) / "outer"
        unpack(path, kind, root)
        if args.inner:
            nested = root / args.inner
            root = Path(tmp) / "inner"
            unpack(nested, archive_kind_of(args.inner), root)
        bundles = find_bundles(root)
        print("bundles", json.dumps(bundles, indent=2) if bundles else "(none)")
        if not bundles and not args.inner:
            print("nested archives (retry with --inner PATH):")
            for n in nested_archives(root):
                print("  ", n)
    return 0


def cmd_outdated(_args) -> int:
    token = os.environ.get("GITHUB_TOKEN")
    for _, m in load_manifests():
        repo = "/".join(m["repo"].rstrip("/").split("/")[-2:])
        release_repo = m.get("releases", repo)  # some projects publish binaries from another repo
        req = urllib.request.Request(f"https://api.github.com/repos/{release_repo}/releases/latest",
                                     headers={"User-Agent": UA, **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                tag = json.load(r).get("tag_name", "")
        except Exception as e:  # noqa: BLE001
            print(f"?     {m['id']}: {e}")
            continue
        ours = m["versions"][0]["version"]
        state = "ok   " if tag.lstrip("vV") == ours.lstrip("vV") else "NEWER"
        print(f"{state} {m['id']}: registry {ours}, upstream {tag}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate").set_defaults(fn=cmd_validate)
    b = sub.add_parser("build")
    b.add_argument("--out", default=str(ROOT / "dist"))
    b.set_defaults(fn=cmd_build)
    v = sub.add_parser("verify")
    v.add_argument("ids", nargs="*")
    v.set_defaults(fn=cmd_verify)
    i = sub.add_parser("inspect")
    i.add_argument("source", help="release asset URL or local file")
    i.add_argument("--archive", choices=sorted(ARCHIVES))
    i.add_argument("--inner", help="nested archive path to expand as well")
    i.set_defaults(fn=cmd_inspect)
    sub.add_parser("outdated").set_defaults(fn=cmd_outdated)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
