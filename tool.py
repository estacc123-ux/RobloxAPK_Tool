"""
Modes:
  python roblox_tool.py                  # download → decompile (full pipeline)
  python roblox_tool.py --only download  # download + inspect only
  python roblox_tool.py --only extract   # decompile already-downloaded APKs
  python roblox_tool.py --inspect <file> # quick inspect of any APK/XAPK

Requirements (set DEFAULT_JADX and DEFAULT_APKTOOL paths at the top of the script):
  jadx    → https://github.com/skylot/jadx/releases
  apktool → https://apktool.org/docs/install
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup

# output dirs live right next to this script - no config needed, it just works
DEFAULT_SAVE_DIR = Path(__file__).parent / "downloads"
DEFAULT_OUTPUT   = Path(__file__).parent / "downloads" / "roblox_src"

# point these at wherever you installed jadx and apktool on your machine
DEFAULT_JADX    = r"C:\Users\<Name>\Downloads\JADX\jadx-<version>\bin\jadx.bat" 
DEFAULT_APKTOOL = r"C:\Apktool\apktool.bat" # i put mine mine at "C:" dont ask 

PACKAGE       = "com.roblox.client"
APKCOMBO_BASE = "https://apkcombo.com"

# pretending to be a real browser - most APK sites will flat out ignore bots
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

ARCH_DIRS = {
    "arm64-v8a":   "ARM64 (64-bit)  - modern Android phones (2018+)",
    "armeabi-v7a": "ARM32 (32-bit)  - older/budget Android phones",
    "x86_64":      "x86_64 (64-bit) - Intel/AMD emulator",
    "x86":         "x86    (32-bit) - Intel emulator",
}

# lower number = higher priority when deciding which APKs to process first
APK_PRIORITY: dict[str, int] = {
    "base": 0, "arm64_v8a": 1, "armeabi_v7a": 2,
    "x86_64": 3, "x86": 4, "en": 5,
    "xxhdpi": 6, "xhdpi": 7, "hdpi": 8, "mdpi": 9, "ldpi": 10,
}

_LANG_CODES = frozenset([
    "en","fr","de","es","ja","ko","zh","pt","it",
    "ru","ar","tr","nl","pl","sv","da","fi","no",
])
_DENSITY_KEYS = frozenset([
    "ldpi","mdpi","hdpi","xhdpi","xxhdpi","xxxhdpi","tvdpi",
])

# apktool loves to print noise that helps nobody - filter it out before it hits stdout
_SKIP_PREFIXES = (
    "S: Could not decode",
    "Press any key",
)


# SECTION 1 - DOWNLOAD (where we go begging the internet for an APK)

def _resolve_url(href: str, base: str) -> str:
    # some sites wrap the real URL in a redirect param - unwrap it
    if href.startswith("/"):
        href = base + href
    parsed = urlparse(href)
    if parsed.path in ("/r2", "/r2/"):
        params = parse_qs(parsed.query)
        if "u" in params:
            return unquote(params["u"][0])
    return href


def _try_apkpure(session: requests.Session) -> Optional[str]:
    print("[*] Trying APKPure...")
    base   = "https://apkpure.com"
    warmup = f"{base}/roblox/{PACKAGE}"
    session.headers.update({"Referer": base + "/"})
    r = session.get(warmup, timeout=15)
    if r.status_code != 200:
        print(f"    Got {r.status_code}, skipping.")
        return None
    time.sleep(1.2)  # be polite, don't hammer them
    session.headers.update({"Referer": warmup})
    r2 = session.get(f"{warmup}/download", timeout=15)
    if r2.status_code != 200:
        print(f"    Download page got {r2.status_code}, skipping.")
        return None
    soup = BeautifulSoup(r2.text, "html.parser")
    tag  = soup.find("a", id="download_link")
    if tag and tag.get("href"):
        return tag["href"]
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".apk" in href.lower() and ("download" in href.lower() or "d.apkpure" in href):
            return href
    return None


def _try_apkcombo(session: requests.Session) -> Optional[str]:
    print("[*] Trying APKCombo...")
    page_url = f"{APKCOMBO_BASE}/roblox/{PACKAGE}/download/apk"
    session.headers.update({"Referer": APKCOMBO_BASE + "/"})
    r = session.get(page_url, timeout=15)
    if r.status_code != 200:
        print(f"    Got {r.status_code}, skipping.")
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a", href=True):
        resolved = _resolve_url(a["href"], APKCOMBO_BASE)
        if ".apk" in resolved.lower():
            return resolved
    return None


def _try_apkmirror(session: requests.Session) -> Optional[str]:
    print("[*] Trying APKMirror...")
    search = "https://www.apkmirror.com/?post_type=app_release&searchtype=apk&s=roblox"
    session.headers.update({"Referer": "https://www.apkmirror.com/"})
    r = session.get(search, timeout=15)
    if r.status_code != 200:
        print(f"    Got {r.status_code}, skipping.")
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "roblox" in href.lower() and "/apk/" in href.lower():
            full = "https://www.apkmirror.com" + href if href.startswith("/") else href
            time.sleep(0.8)
            r2 = session.get(full, timeout=15)
            if r2.status_code != 200:
                continue
            soup2 = BeautifulSoup(r2.text, "html.parser")
            dl    = soup2.find("a", {"data-google-vignette": "false"}, href=True)
            if dl:
                dh = dl["href"]
                return "https://www.apkmirror.com" + dh if dh.startswith("/") else dh
    return None


def _fetch_download_url() -> str:
    # three sources tried in order - if all three fail, something has gone terribly wrong
    session = requests.Session()
    session.headers.update(HEADERS)
    for fn in [_try_apkpure, _try_apkcombo, _try_apkmirror]:
        try:
            url = fn(session)
        except Exception as e:
            print(f"    Error: {e}")
            url = None
        if url:
            return url
        print()
    print("[!] All download sources failed. The internet has abandoned us.")
    print("    Download manually: https://apkcombo.com/roblox/com.roblox.client")
    sys.exit(1)


def download_apk(save_dir: Path) -> Path:
    url = _fetch_download_url()

    save_dir.mkdir(parents=True, exist_ok=True)
    path_part = urlparse(url).path
    filename  = os.path.basename(path_part)
    if not filename.lower().endswith((".apk", ".xapk")):
        filename = "Roblox_latest.apk"
    save_path = save_dir / filename

    print(f"\n[*] Downloading:\n    {url}\n")
    session = requests.Session()
    session.headers.update({**HEADERS, "Referer": url})

    with session.get(url, stream=True, timeout=120, allow_redirects=True) as r:
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "")
        if "html" in ct:
            # getting HTML back means the site wants JS rendered - or wants us gone
            print("[!] Got HTML back instead of a file. They're onto us.")
            sys.exit(1)
        total      = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(save_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct    = downloaded / total * 100
                        filled = int(pct // 2)
                        bar    = "#" * filled + "-" * (50 - filled)
                        print(
                            f"\r    [{bar}] {pct:5.1f}%  "
                            f"({downloaded // 1048576} / {total // 1048576} MB)",
                            end="", flush=True,
                        )

    size_mb = save_path.stat().st_size / 1048576
    print(f"\n\n[+] Done! ({size_mb:.1f} MB of corporate secrets, probably)")
    print(f"    Saved to: {save_path.resolve()}")
    return save_path


# SECTION 2 - APK INSPECT (cracking it open and having a look around)

def _sniff_package(z: zipfile.ZipFile) -> Optional[str]:
    # AndroidManifest.xml is binary-encoded inside APKs, but the package name
    # is still plain ASCII - we just scan for it directly rather than full parsing
    if "AndroidManifest.xml" not in z.namelist():
        return None
    raw = z.read("AndroidManifest.xml")
    idx = raw.find(b"com.roblox")
    if idx == -1:
        return None
    end = idx
    while end < len(raw) and raw[end] not in (0, 0x20):
        end += 1
    return raw[idx:end].decode("ascii", errors="ignore").strip()


def inspect_apk(path: Path) -> dict:
    print("\n" + "=" * 58)
    print("  APK INSPECTION")
    print("=" * 58)

    meta: dict = {"valid": False, "type": None, "archs": [],
                  "package": None, "version": None, "inner_apks": []}

    if not zipfile.is_zipfile(path):
        print("[!] Not a valid ZIP/APK - may be corrupted, or just lying to us.")
        return meta

    meta["valid"] = True
    print("[+] Valid ZIP structure")

    with zipfile.ZipFile(path, "r") as z:
        names = z.namelist()
        inner = [n for n in names if n.endswith(".apk")]
        meta["inner_apks"] = inner

        if inner:
            meta["type"] = "xapk"
            print(f"[*] Type        : XAPK (split bundle - {len(inner)} inner APKs)")
            for n in inner:
                print(f"    • {n}")
        else:
            meta["type"] = "apk"
            print("[*] Type        : Universal APK")

        # check which CPU architectures have native libs - this matters when
        # you want to know which .so files will actually run on a given device
        found = [a for a in ARCH_DIRS if any(n.startswith(f"lib/{a}/") for n in names)]
        if not found and inner:
            for inner_name in inner:
                data = z.read(inner_name)
                with zipfile.ZipFile(io.BytesIO(data)) as iz:
                    for a in ARCH_DIRS:
                        if a not in found and any(n.startswith(f"lib/{a}/") for n in iz.namelist()):
                            found.append(a)
        meta["archs"] = found
        if found:
            print(f"\n[+] Architecture(s):")
            for a in found:
                print(f"    • {ARCH_DIRS[a]}")

        pkg = _sniff_package(z)
        meta["package"] = pkg
        if pkg:
            ok = "✓ official Roblox" if PACKAGE in pkg else "✗ UNEXPECTED - who is this?"
            print(f"\n[+] Package     : {pkg}  ({ok})")

        vm = re.search(r"(\d+\.\d+[\.\d]*)", path.name)
        if vm:
            meta["version"] = vm.group(1)
            print(f"[+] Version     : {meta['version']}")

        dex = sum(1 for n in names if n.endswith(".dex"))
        so  = sum(1 for n in names if n.endswith(".so"))
        print(f"[+] DEX/SO      : {dex} dex, {so} .so - {len(names)} total entries")

    print("=" * 58)
    return meta


# SECTION 3 - EXTRACTOR (the part that actually does the heavy lifting)

def _is_lang_split(name: str) -> bool:
    if any(k in name for k in ("arm64", "armeabi", "x86")):
        return False
    for lc in _LANG_CODES:
        if re.search(rf'[_\.]{re.escape(lc)}[_\.]', name):
            return True
        if re.search(rf'[_\.]{re.escape(lc)}\.apk$', name):
            return True
    return False


def classify_apk(path: Path) -> dict:
    name = path.name.lower()
    stem = path.stem.lower()
    is_base    = "base" in stem or ("split" not in stem and "config" not in stem)
    is_arm64   = "arm64" in name
    is_arm32   = "armeabi" in name and "arm64" not in name
    is_lang    = _is_lang_split(name)
    is_density = any(d in name for d in _DENSITY_KEYS)
    priority   = next((v for k, v in APK_PRIORITY.items() if k in name), 99)
    if is_base:
        priority = 0
    return {
        "path": path, "name": path.name,
        "is_base": is_base, "is_arm64": is_arm64, "is_arm32": is_arm32,
        "is_lang": is_lang, "is_density": is_density, "priority": priority,
    }


def _is_framework_apk(p: Path) -> bool:
    if "framework" in [part.lower() for part in p.parts]:
        return True
    try:
        int(p.stem)  # numeric filenames are Android framework stubs - not our problem
        return True
    except ValueError:
        return False


def scan_apks(scan_dir: Path, *,
              include_arm32=False,
              include_lang=False,
              include_density=False) -> list[dict]:
    all_apks   = list(scan_dir.rglob("*.apk"))
    real_apks  = [p for p in all_apks if not _is_framework_apk(p) and zipfile.is_zipfile(p)]
    classified = sorted([classify_apk(p) for p in real_apks], key=lambda x: x["priority"])
    selected, skipped = [], []

    for apk in classified:
        reason = None
        if   apk["is_lang"]    and not include_lang:    reason = "language split"
        elif apk["is_density"] and not include_density: reason = "density split"
        elif apk["is_arm32"]   and not include_arm32:   reason = "ARM32 split"
        (skipped if reason else selected).append(apk if not reason else (apk["name"], reason))

    print(f"\n[*] Found {len(all_apks)} APK(s) → {len(selected)} selected")
    for apk in selected:
        tag = "BASE " if apk["is_base"] else ("ARM64" if apk["is_arm64"] else "OTHER")
        print(f"    [{tag}]  {apk['name']}")
    for name, reason in skipped:
        print(f"    [SKIP]  {name}  ({reason}, don't need it)")
    return selected


def require_tool(name: str, path: str) -> str:
    # just use the path directly - if it's wrong, fix DEFAULT_JADX / DEFAULT_APKTOOL up top
    if not Path(path).exists():
        print(f"[✗] '{name}' not found at: {path}")
        print(f"    Update the DEFAULT_{name.upper()} path at the top of the script.")
        if name == "jadx":
            print(f"    → https://github.com/skylot/jadx/releases")
        elif name == "apktool":
            print(f"    → https://apktool.org/docs/install")
        sys.exit(1)
    print(f"[✓] {name}: {path}")
    return path


def run_tool(cmd: list, label: str, out_dir: Optional[Path] = None) -> dict:
    print(f"\n[>] {label}")
    print(f"    $ {' '.join(str(c) for c in cmd)}\n")
    log = {
        "label": label, "cmd": " ".join(str(c) for c in cmd),
        "exit_code": None, "status": None,
        "errors": [], "warnings": [], "file_count": 0,
        "started_at": datetime.now().isoformat(), "ended_at": None,
    }
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, bufsize=1,
        )
    except FileNotFoundError as e:
        log["status"] = "failed"
        log["errors"].append(str(e))
        print(f"[✗] Failed to launch: {e}")
        return log

    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        if any(line.strip().startswith(p) for p in _SKIP_PREFIXES):
            log["warnings"].append(line)
            continue
        print(f"    {line}")
        ll = line.upper()
        if "ERROR" in ll:
            log["errors"].append(line)
        elif "WARN" in ll:
            log["warnings"].append(line)

    proc.wait()
    log["exit_code"] = proc.returncode
    log["ended_at"]  = datetime.now().isoformat()
    if out_dir and out_dir.exists():
        log["file_count"] = sum(1 for f in out_dir.rglob("*") if f.is_file())

    if proc.returncode == 0:
        log["status"] = "ok"
        print(f"\n[✓] {label} - done, nice")
    elif log["file_count"] > 0:
        # non-zero exit but files were produced - tool complained but still delivered
        log["status"] = "partial"
        print(f"\n[~] {label}: grumbled but produced {log['file_count']} file(s) - we'll take it")
    else:
        log["status"] = "failed"
        print(f"\n[✗] {label}: FAILED (exit {proc.returncode}) - this is fine 🔥")
    return log


def copy_so_files(apk_path: Path, dest: Path) -> int:
    # .so files are the compiled native libraries - a lot of the interesting
    # low-level logic lives in here, which is exactly why we want them
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(apk_path, "r") as zf:
        for info in zf.infolist():
            if not info.filename.endswith(".so"):
                continue
            out = dest / Path(info.filename).name
            with zf.open(info) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
            print(f"    [so] {info.filename}  ({info.file_size:,} bytes)")
            count += 1
    if count:
        print(f"[✓] Pulled {count} .so file(s) → {dest}")
    else:
        print(f"[i] No .so files in {apk_path.name} - nothing to grab here")
    return count


def _size_str(path: Path) -> str:
    try:
        total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024:
            return f"{total:.1f} {unit}"
        total /= 1024
    return f"{total:.1f} TB"


def _make_section(log: dict, path: Path) -> dict:
    return {
        "path":     str(path) if log["status"] in ("ok", "partial") else "FAILED",
        "status":   log["status"],
        "files":    log["file_count"],
        "errors":   len(log["errors"]),
        "warnings": len(log["warnings"]),
        "log": {k: log.get(k) for k in
                ("status","exit_code","file_count","errors","warnings","started_at","ended_at")},
    }


def process_apk(apk_info: dict, out_root: Path, jadx_bin: Optional[str],
                apktool_bin: Optional[str], tasks: set, verbose: bool) -> dict:
    apk_path = apk_info["path"]
    apk_out  = out_root / apk_path.stem
    apk_out.mkdir(parents=True, exist_ok=True)

    tag = "BASE" if apk_info["is_base"] else ("ARM64" if apk_info["is_arm64"] else "SPLIT")
    print(f"\n{'='*64}")
    print(f"  [{tag}]  {apk_path.name}")
    print(f"  Output : {apk_out}")
    print(f"{'='*64}")

    results: dict = {}

    if "java" in tasks and apk_info["is_base"]:
        java_out = apk_out / "java_src"
        cmd = [
            jadx_bin,
            "--output-dir", str(java_out),
            "--deobf",           # attempt to undo obfuscation - works maybe 60% of the time
            "--show-bad-code",   # include methods jadx couldn't fully reconstruct - still useful
            "--threads-count", str(min(os.cpu_count() or 4, 8)),
            str(apk_path),
        ]
        if verbose:
            cmd.append("--verbose")
        log = run_tool(cmd, "JADX - Java/Kotlin decompile", out_dir=java_out)
        results["java_src"] = _make_section(log, java_out)
    elif "java" in tasks:
        print("[i] Skipping JADX for split APK - nothing useful in there anyway")

    if "xml" in tasks or "assets" in tasks:
        res_out = apk_out / "resources"
        cmd = [
            apktool_bin, "d", str(apk_path),
            "--output", str(res_out),
            "--force", "--no-src",
            # separate framework path per APK prevents cross-contamination between runs
            "--frame-path", str(apk_out / "framework"),
        ]
        log = run_tool(cmd, "apktool - resources & XML", out_dir=res_out)
        results["resources"] = _make_section(log, res_out)

    if "so" in tasks:
        so_out   = apk_out / "native_libs"
        so_count = copy_so_files(apk_path, so_out)
        results["native_libs"] = {
            "path":   str(so_out),
            "status": "ok" if so_count > 0 else "empty",
            "files":  so_count,
        }

    return results


def print_summary(all_results: dict, out_root: Path) -> None:
    print(f"\n{'='*64}")
    print("  SUMMARY")
    print(f"{'='*64}")
    for apk_name, sections in all_results.items():
        print(f"\n  {apk_name}")
        for section, data in sections.items():
            if not isinstance(data, dict):
                continue
            path   = data.get("path", "FAILED")
            status = data.get("status", "?")
            files  = data.get("files", 0)
            errors = data.get("errors", 0)
            warns  = data.get("warnings", 0)
            if status == "failed" or path == "FAILED":
                print(f"    [✗] {section:<20} FAILED  ¯\\_(ツ)_/¯")
            elif status == "partial":
                sz = _size_str(Path(path)) if Path(path).exists() else "?"
                print(f"    [~] {section:<20} {path}  ({sz}, {files} files, {errors} err, {warns} warn)")
            else:
                sz = _size_str(Path(path)) if Path(path).exists() else "?"
                print(f"    [✓] {section:<20} {path}  ({sz}, {files} files)")

    # write a JSON report so you can diff runs, grep for errors, or just feel organized
    report = out_root / "decompile_report.json"
    with open(report, "w", encoding="utf-8") as f:
        json.dump({"timestamp": datetime.now().isoformat(), "results": all_results},
                  f, indent=2, default=str)
    print(f"\n  Root   : {out_root}")
    print(f"  Report : {report}")
    print(f"{'='*64}")


def maybe_unpack_xapk(apk_path: Path) -> Path:
    """If it's an XAPK, crack it open and pull out the inner APKs."""
    if not zipfile.is_zipfile(apk_path):
        return apk_path.parent
    with zipfile.ZipFile(apk_path, "r") as z:
        inner = [n for n in z.namelist() if n.endswith(".apk")]
        if not inner:
            return apk_path.parent
        extract_dir = apk_path.parent / (apk_path.stem + "_splits")
        extract_dir.mkdir(exist_ok=True)
        for name in inner:
            dest = extract_dir / Path(name).name
            with z.open(name) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            print(f"    [xapk] Extracted: {dest.name}")
        print(f"[+] XAPK splits → {extract_dir}")
        return extract_dir


# SECTION 4 - this part that glues everything together, pray it works and it did just for me 

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Roblox APK Downloader + Decompiler",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Examples:
  python roblox_tool.py                            # full pipeline (default)
  python roblox_tool.py --only download            # download + inspect only
  python roblox_tool.py --only extract             # decompile existing APKs
  python roblox_tool.py --inspect Roblox.apk       # quick inspect
  python roblox_tool.py --tasks so                 # only extract .so files
  python roblox_tool.py --scan C:\\APKs -o C:\\out  # custom paths
""")
    p.add_argument("--only",     choices=["download", "extract"], default=None)
    p.add_argument("--inspect",  metavar="FILE")
    p.add_argument("--scan",     default=str(DEFAULT_SAVE_DIR), metavar="DIR")
    p.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT), metavar="DIR")
    p.add_argument("--save-dir", default=str(DEFAULT_SAVE_DIR), metavar="DIR")
    p.add_argument("--tasks",    nargs="+",
                   choices=["java","xml","assets","so"],
                   default=["java","xml","assets","so"])
    p.add_argument("--jadx",    default=None, metavar="PATH",
                   help="path to jadx binary (default: DEFAULT_JADX in script)")
    p.add_argument("--apktool", default=None, metavar="PATH",
                   help="path to apktool binary (default: DEFAULT_APKTOOL in script)")
    p.add_argument("--include-arm32",   action="store_true")
    p.add_argument("--include-lang",    action="store_true")
    p.add_argument("--include-density", action="store_true")
    p.add_argument("-v", "--verbose",   action="store_true")
    return p.parse_args()


def main() -> None:
    args  = parse_args()
    tasks = set(args.tasks)
    if "assets" in tasks:
        tasks.add("xml")  # assets live inside resources, so xml is implicitly required
    out_root = Path(args.output).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    if args.inspect:
        inspect_apk(Path(args.inspect))
        return

    do_download = args.only in (None, "download")
    do_extract  = args.only in (None, "extract")

    downloaded_path: Optional[Path] = None

    if do_download:
        print("\n" + "━"*64)
        print("  STAGE 1 - DOWNLOAD")
        print("━"*64)
        save_dir        = Path(args.save_dir).resolve()
        downloaded_path = download_apk(save_dir)
        meta            = inspect_apk(downloaded_path)
        if meta.get("type") == "xapk":
            scan_target = maybe_unpack_xapk(downloaded_path)
        else:
            scan_target = save_dir
    else:
        scan_target = Path(args.scan).resolve()

    if not do_extract:
        return

    print("\n" + "━"*64)
    print("  STAGE 2 - DECOMPILE")
    print("━"*64)
    print(f"[*] Scan dir : {scan_target}")
    print(f"[*] Output   : {out_root}")

    # resolve tools upfront so we fail fast before doing any real work
    jadx_bin    = require_tool("jadx",    args.jadx    or DEFAULT_JADX)    if "java" in tasks else None
    apktool_bin = require_tool("apktool", args.apktool or DEFAULT_APKTOOL) if "xml"  in tasks else None

    selected = scan_apks(
        scan_target,
        include_arm32   = args.include_arm32,
        include_lang    = args.include_lang,
        include_density = args.include_density,
    )
    if not selected:
        print("[!] No APKs selected - nothing to decompile. Enjoy your evening.")
        sys.exit(0)

    print(f"\n[*] Processing {len(selected)} APK(s)…\n")
    all_results: dict = {}
    for apk_info in selected:
        all_results[apk_info["name"]] = process_apk(
            apk_info    = apk_info,
            out_root    = out_root,
            jadx_bin    = jadx_bin,
            apktool_bin = apktool_bin,
            tasks       = tasks,
            verbose     = args.verbose,
        )

    print_summary(all_results, out_root)


if __name__ == "__main__":
    main()
