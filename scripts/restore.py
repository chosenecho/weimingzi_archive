#!/usr/bin/env python3
"""Download, verify, and restore release assets for weimingzi_archive.

Pure Python standard library implementation.
Works both online (downloading directly from GitHub Releases without requiring
`gh` CLI) and offline (restoring from a local directory containing downloaded
`.tar.gz`, `MANIFEST.json`, and `SHA256SUMS.txt`).
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "chosenecho/weimingzi_archive"
USER_AGENT = "weimingzi_archive-restore/1.0"
DOWNLOAD_CHUNK_BYTES = 256 * 1024
DOWNLOAD_TIMEOUT_SEC = 45
DOWNLOAD_RETRIES = 6
DOWNLOAD_MAX_TOTAL_ATTEMPTS = 25


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def format_size(num_bytes: int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    val = float(num_bytes)
    for u in units:
        if val < 1024.0 or u == units[-1]:
            return f"{val:.2f} {u}" if u != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{num_bytes} B"


def resolve_latest_tag(repo: str) -> str:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    last_err: Exception | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            tag = data.get("tag_name")
            if not tag:
                raise RuntimeError(f"Could not determine latest release tag for {repo}")
            return str(tag)
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
            last_err = e
            if attempt < DOWNLOAD_RETRIES:
                wait = min(2 ** attempt, 15)
                print(
                    f"    Retry {attempt}/{DOWNLOAD_RETRIES - 1} resolving latest tag after {wait}s ({e})",
                    file=sys.stderr,
                )
                time.sleep(wait)
    assert last_err is not None
    raise last_err


def download_file(url: str, dest: Path, retries: int = DOWNLOAD_RETRIES) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_err: Exception | None = None
    consecutive_failures = 0
    total_attempts = 0

    while consecutive_failures < retries and total_attempts < DOWNLOAD_MAX_TOTAL_ATTEMPTS:
        total_attempts += 1
        existing = tmp.stat().st_size if tmp.exists() else 0
        written = existing
        headers = {"User-Agent": USER_AGENT}
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:
                status = getattr(resp, "status", 200)
                if existing > 0 and status == 206:
                    mode = "ab"
                    written = existing
                    content_len = int(resp.headers.get("Content-Length") or 0)
                    total = existing + content_len if content_len > 0 else 0
                else:
                    mode = "wb"
                    written = 0
                    total = int(resp.headers.get("Content-Length") or 0)

                with tmp.open(mode) as f:
                    while True:
                        chunk = resp.read(DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        f.write(chunk)
                        written += len(chunk)
                        if total > 0:
                            pct = written * 100.0 / total
                            print(
                                f"\r    {dest.name}: {format_size(written)} / {format_size(total)} ({pct:5.1f}%)",
                                end="",
                                flush=True,
                            )
                if total > 0:
                    print()
                if total > 0 and written < total:
                    raise http.client.IncompleteRead(b"", total - written)
            tmp.replace(dest)
            return
        except urllib.error.HTTPError as e:
            if e.code == 416 and tmp.exists():
                tmp.unlink(missing_ok=True)
                continue
            if 400 <= e.code < 500 and e.code != 429:
                tmp.unlink(missing_ok=True)
                raise
            last_err = e
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
            last_err = e

        if written > existing:
            # Forward progress was made; reset consecutive failure counter so Range resume can finish.
            consecutive_failures = 0
        else:
            consecutive_failures += 1

        if consecutive_failures < retries and total_attempts < DOWNLOAD_MAX_TOTAL_ATTEMPTS:
            wait = min(2 ** max(consecutive_failures, 1), 15)
            print(
                f"\n    [WARN] Download interrupted ({last_err}); resuming in {wait}s...",
                file=sys.stderr,
            )
            time.sleep(wait)

    if tmp.exists():
        tmp.unlink(missing_ok=True)
    assert last_err is not None
    raise last_err


def parse_checksums(sums_path: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw_line in sums_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            digest, fname = parts[0].strip().lower(), parts[1].strip().lstrip("*")
            mapping[fname] = digest
    return mapping


def select_assets(
    manifest: dict,
    preset: str,
    groups: list[str] | None,
    modules: list[str] | None,
) -> list[dict]:
    selected: list[dict] = []
    for asset in manifest.get("assets", []):
        tier = asset.get("tier", "full")
        kind = asset.get("kind", "")
        grp = asset.get("group", "")
        mod = asset.get("module", "asr")

        if modules and mod not in modules:
            continue

        if preset == "light" and tier != "light":
            continue
        if preset == "db" and kind != "sqlite-db":
            continue
        # preset == "full" keeps all tiers

        if groups and grp != "_meta" and grp not in groups:
            continue

        selected.append(asset)
    return selected


def safe_extract_tar_gz(archive_path: Path, dest_dir: Path) -> int:
    """Safely extract a .tar.gz archive preventing path traversal or link escape."""
    dest_resolved = dest_dir.resolve()
    extracted = 0
    with tarfile.open(archive_path, mode="r:gz") as tf:
        members = tf.getmembers()
        for m in members:
            if m.islnk() or m.issym() or m.isdev():
                raise RuntimeError(
                    f"Refusing unsafe link/device member '{m.name}' in {archive_path.name}"
                )
            target = (dest_resolved / m.name).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise RuntimeError(
                    f"Path traversal detected for member '{m.name}' in {archive_path.name}"
                )
            if m.isfile():
                extracted += 1

        if sys.version_info >= (3, 12):
            tf.extractall(path=dest_resolved, filter="data")
        else:
            tf.extractall(path=dest_resolved, members=members)
    return extracted


def verify_file(path: Path, expected_sha256: str) -> None:
    actual = sha256_file(path)
    if actual.lower() != expected_sha256.lower():
        raise RuntimeError(
            f"SHA-256 mismatch for {path.name}: expected {expected_sha256}, got {actual}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download, verify, and restore weimingzi_archive release assets."
    )
    parser.add_argument(
        "--tag",
        default="latest",
        help="Release tag to download (e.g. v2026.11.27 or 'latest'). Ignored if --src-dir is set.",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"GitHub repository (default: {DEFAULT_REPO})",
    )
    parser.add_argument(
        "--src-dir",
        type=Path,
        default=None,
        help="Restore offline from a local directory containing downloaded release files.",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=REPO_ROOT,
        help=f"Destination root directory (default: {REPO_ROOT})",
    )
    parser.add_argument(
        "--preset",
        choices=("light", "full", "db"),
        default="light",
        help="light: text+srt + db (default); full: all assets including word-level JSON; db: database only",
    )
    parser.add_argument(
        "--groups",
        nargs="+",
        help="Optional group filter, e.g. bili_23191782 yt_wujixuanshi",
    )
    parser.add_argument(
        "--modules",
        nargs="+",
        help="Optional module filter, e.g. asr",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Keep downloaded release files in this directory instead of a temporary directory.",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip SHA-256 checksum verification (not recommended).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Only list matching assets and sizes without downloading/extracting archives.",
    )

    args = parser.parse_args()
    dest_dir: Path = args.dest
    dest_dir.mkdir(parents=True, exist_ok=True)

    tmp_ctx = None
    try:
        if args.src_dir is not None:
            work_dir = args.src_dir
            if not work_dir.is_dir():
                raise FileNotFoundError(f"Source directory not found: {work_dir}")
            base_url = None
            tag = args.tag
        else:
            tag = resolve_latest_tag(args.repo) if args.tag == "latest" else args.tag
            base_url = f"https://github.com/{args.repo}/releases/download/{tag}"
            if args.cache_dir is not None:
                work_dir = args.cache_dir
                work_dir.mkdir(parents=True, exist_ok=True)
            else:
                tmp_ctx = tempfile.TemporaryDirectory(prefix="wmz_restore_")
                work_dir = Path(tmp_ctx.name)

            print(f"Fetching manifest for {args.repo}@{tag}...")
            for meta_name in ("MANIFEST.json", "SHA256SUMS.txt"):
                meta_path = work_dir / meta_name
                if not meta_path.is_file():
                    download_file(f"{base_url}/{meta_name}", meta_path)

        manifest_path = work_dir / "MANIFEST.json"
        sums_path = work_dir / "SHA256SUMS.txt"

        if not manifest_path.is_file():
            raise FileNotFoundError(f"Missing MANIFEST.json in {work_dir}")

        checksums = parse_checksums(sums_path) if sums_path.is_file() else {}
        if not args.skip_verify and "MANIFEST.json" in checksums:
            verify_file(manifest_path, checksums["MANIFEST.json"])

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assets = select_assets(manifest, args.preset, args.groups, args.modules)
        if not assets:
            print("No assets matched the selected preset/filters.", file=sys.stderr)
            return 1

        total_dl = sum(a["archive_bytes"] for a in assets)
        total_raw = sum(a["raw_bytes"] for a in assets)
        print(
            f"Matched {len(assets)} asset(s) for preset='{args.preset}' "
            f"(archive={format_size(total_dl)}, extracted={format_size(total_raw)}):"
        )
        for a in assets:
            print(
                f"  - {a['name']} [{a.get('tier', '-')}/{a.get('kind', '-')}] "
                f"({format_size(a['archive_bytes'])}, {a['files']:,} files)"
            )

        if args.list:
            return 0

        total_files = 0
        for idx, asset in enumerate(assets, 1):
            name = asset["name"]
            arc_path = work_dir / name
            expected_hash = checksums.get(name) or asset.get("sha256")

            if not arc_path.is_file():
                if base_url is None:
                    raise FileNotFoundError(
                        f"Missing local archive {arc_path} (offline --src-dir mode)."
                    )
                print(f"[{idx}/{len(assets)}] Downloading {name}...")
                download_file(f"{base_url}/{name}", arc_path)
            else:
                print(f"[{idx}/{len(assets)}] Using cached {name}")

            if not args.skip_verify:
                if not expected_hash:
                    raise RuntimeError(f"No SHA-256 checksum available for {name}")
                verify_file(arc_path, expected_hash)
                print(f"    [OK] SHA-256 verified")

            cnt = safe_extract_tar_gz(arc_path, dest_dir)
            total_files += cnt
            print(f"    [OK] Extracted {cnt:,} file(s) -> {dest_dir}")

        print(
            f"Restore complete: {len(assets)} archive(s), {total_files:,} file(s) "
            f"restored under {dest_dir / 'data'}"
        )
        return 0
    except urllib.error.HTTPError as e:
        print(f"HTTP error {e.code} while accessing GitHub Releases: {e.url}", file=sys.stderr)
        return 1
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
