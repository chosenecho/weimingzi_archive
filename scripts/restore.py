#!/usr/bin/env python3
"""Download, verify, and restore release assets for weimingzi_archive.

Pure Python standard library implementation.
Supports independent pipeline release streams (`asr-vYYYY.MM.DD` and
`archive-vYYYY.MM.DD`) plus incremental time-range delta releases
(`<module>-delta-YYYY.MM.DD-YYYY.MM.DD`), automatic latest-tag resolution
per module, SQLite delta auto-merge, HTTP Range resume with retry/backoff,
SHA-256 verification, and offline `--src-dir` mode.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import sqlite3
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "chosenecho/weimingzi_archive"
USER_AGENT = "weimingzi_archive-restore/2.1"
DOWNLOAD_CHUNK_BYTES = 256 * 1024
DOWNLOAD_TIMEOUT_SEC = 45
DOWNLOAD_RETRIES = 6
DOWNLOAD_MAX_TOTAL_ATTEMPTS = 25

KNOWN_MODULES: tuple[str, ...] = ("asr", "archive")
MODULE_BASE_PREFIXES: dict[str, tuple[str, ...]] = {
    "asr": ("asr-base-", "asr-v"),
    "archive": ("archive-base-", "archive-v"),
}
MODULE_DELTA_PREFIX: dict[str, str] = {
    "asr": "asr-delta-",
    "archive": "archive-delta-",
}
BASE_RANGE_TAG_RE = re.compile(
    r"^(?:asr|archive)-base-(\d{4}\.\d{2}\.\d{2})-(\d{4}\.\d{2}\.\d{2})(?:-r(\d+))?$"
)
LEGACY_BASE_TAG_RE = re.compile(
    r"^(?:asr|archive)-v(\d{4}\.\d{2}\.\d{2})(?:-r(\d+))?$"
)
DELTA_TAG_RE = re.compile(
    r"^(?:asr|archive)-delta-(\d{4}\.\d{2}\.\d{2})-(\d{4}\.\d{2}\.\d{2})(?:-r(\d+))?$"
)


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


def fetch_json_with_retry(url: str) -> object:
    last_err: Exception | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ) as e:
            last_err = e
            if attempt < DOWNLOAD_RETRIES:
                wait = min(2**attempt, 15)
                print(
                    f"    Retry {attempt}/{DOWNLOAD_RETRIES - 1} fetching {url} after {wait}s ({e})",
                    file=sys.stderr,
                )
                time.sleep(wait)
    assert last_err is not None
    raise last_err


def resolve_latest_tags_by_module(
    repo: str,
    requested_modules: list[str],
    include_deltas: bool = True,
) -> list[tuple[str, str]]:
    """Resolve newest non-draft base release + applicable delta releases per module."""
    releases = fetch_json_with_retry(
        f"https://api.github.com/repos/{repo}/releases?per_page=50"
    )
    if not isinstance(releases, list):
        raise RuntimeError(f"Unexpected GitHub releases API response for {repo}")

    resolved: list[tuple[str, str]] = []
    for mod in requested_modules:
        prefixes = MODULE_BASE_PREFIXES[mod]
        base_candidates: list[tuple[str, int, str, str]] = []
        for r in releases:
            if not isinstance(r, dict) or r.get("draft"):
                continue
            tag = str(r.get("tag_name") or "")
            if not tag.startswith(prefixes):
                continue
            m_range = BASE_RANGE_TAG_RE.match(tag)
            m_legacy = LEGACY_BASE_TAG_RE.match(tag)
            if m_range:
                end_date = m_range.group(2)
                rev = int(m_range.group(3) or 1)
            elif m_legacy:
                end_date = m_legacy.group(1)
                rev = int(m_legacy.group(2) or 1)
            else:
                continue
            pub_at = str(r.get("published_at") or r.get("created_at") or "")
            base_candidates.append((end_date, rev, tag, pub_at))

        if not base_candidates:
            continue

        base_date, _, base_tag, base_pub = max(base_candidates, key=lambda x: (x[0], x[1], x[2]))
        resolved.append((mod, base_tag))

        if include_deltas:
            dprefix = MODULE_DELTA_PREFIX[mod]
            delta_candidates: list[tuple[str, str, int, str]] = []
            for r in releases:
                if not isinstance(r, dict) or r.get("draft"):
                    continue
                tag = str(r.get("tag_name") or "")
                if not tag.startswith(dprefix):
                    continue
                m = DELTA_TAG_RE.match(tag)
                if not m:
                    continue
                d_start, d_end, d_rev = m.group(1), m.group(2), int(m.group(3) or 1)
                d_pub = str(r.get("published_at") or r.get("created_at") or "")
                if d_end > base_date or (d_end == base_date and d_pub >= base_pub):
                    delta_candidates.append((d_start, d_end, d_rev, tag))
            for _, _, _, dtag in sorted(delta_candidates):
                resolved.append((mod, dtag))

    if not resolved:
        raise RuntimeError(
            f"No published releases found on {repo} for module(s): {', '.join(requested_modules)}"
        )
    return resolved


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
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ) as e:
            last_err = e

        if written > existing:
            consecutive_failures = 0
        else:
            consecutive_failures += 1

        if (
            consecutive_failures < retries
            and total_attempts < DOWNLOAD_MAX_TOTAL_ATTEMPTS
        ):
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
    tiers: list[str] | None = None,
) -> list[dict]:
    selected: list[dict] = []
    for asset in manifest.get("assets", []):
        tier = asset.get("tier", "full")
        kind = asset.get("kind", "")
        grp = asset.get("group", "")
        mod = asset.get("module", "asr")

        if modules and mod not in modules:
            continue

        if tiers:
            if tier not in tiers:
                continue
        else:
            if preset == "light" and tier != "light":
                continue
            elif preset == "media" and tier not in ("light", "images"):
                continue
            elif preset == "raw" and tier not in ("light", "raw", "full"):
                continue
            elif preset == "db" and kind != "sqlite-db":
                continue
            # preset == "full" includes all tiers

        if groups and grp != "_meta" and grp not in groups:
            continue

        selected.append(asset)
    return selected


def safe_extract_tar_gz(archive_path: Path, dest_dir: Path) -> tuple[int, list[str]]:
    """Safely extract a .tar.gz archive preventing path traversal or link escape."""
    dest_resolved = dest_dir.resolve()
    extracted = 0
    extracted_paths: list[str] = []
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
                extracted_paths.append(m.name.replace("\\", "/"))

        if sys.version_info >= (3, 12):
            tf.extractall(path=dest_resolved, filter="data")
        else:
            tf.extractall(path=dest_resolved, members=members)
    return extracted, extracted_paths


def merge_delta_sqlite_db(delta_rel: str, dest_dir: Path, module: str) -> str:
    """Merge an extracted delta SQLite DB into the base DB if present."""
    delta_path = dest_dir / delta_rel
    if not delta_path.is_file():
        return f"Delta DB not found at {delta_rel}"

    if module == "asr":
        base_path = dest_dir / "data" / "db" / "asr_pipeline.db"
        if not base_path.is_file():
            return f"Standalone delta DB saved at {delta_rel} (base asr_pipeline.db not present)"
        scon = sqlite3.connect(f"file:{delta_path}?mode=ro", uri=True)
        dcon = sqlite3.connect(str(base_path))
        try:
            scols = [r[1] for r in scon.execute("PRAGMA table_info(media_items)").fetchall()]
            dcols = [r[1] for r in dcon.execute("PRAGMA table_info(media_items)").fetchall()]
            common = [c for c in scols if c in dcols and c != "id"]
            rows = scon.execute(f"SELECT {','.join(common)} FROM media_items").fetchall()
            grp_i = common.index("source_group")
            vid_i = common.index("video_id")
            vpath_i = common.index("video_path")
            ph = ",".join("?" for _ in common)
            for r in rows:
                dcon.execute(
                    "DELETE FROM media_items WHERE (source_group = ? AND video_id = ?) OR video_path = ?",
                    (r[grp_i], r[vid_i], r[vpath_i]),
                )
                dcon.execute(
                    f"INSERT INTO media_items({','.join(common)}) VALUES ({ph})", r
                )
            dcon.commit()
            return f"Merged delta DB -> data/db/asr_pipeline.db (+{len(rows)} media_items)"
        finally:
            dcon.close()
            scon.close()

    if module == "archive":
        base_path = dest_dir / "data" / "db" / "biliyt.db"
        if not base_path.is_file():
            return f"Standalone delta DB saved at {delta_rel} (base biliyt.db not present)"
        scon = sqlite3.connect(f"file:{delta_path}?mode=ro", uri=True)
        dcon = sqlite3.connect(str(base_path))
        try:
            counts: list[str] = []
            for tbl in ("videos", "dynamics", "articles", "comments", "favorites", "favorite_items"):
                scols = [r[1] for r in scon.execute(f'PRAGMA table_info("{tbl}")').fetchall()]
                dcols = [r[1] for r in dcon.execute(f'PRAGMA table_info("{tbl}")').fetchall()]
                if not scols or not dcols:
                    continue
                common = [c for c in scols if c in dcols and c != "id"]
                rows = scon.execute(f'SELECT {",".join(common)} FROM "{tbl}"').fetchall()
                if not rows:
                    continue
                ph = ",".join("?" for _ in common)
                dcon.executemany(
                    f'INSERT OR REPLACE INTO "{tbl}"({",".join(common)}) VALUES ({ph})',
                    rows,
                )
                counts.append(f"{tbl}={len(rows)}")

            # Merge search_fts idempotently by (target_id, kind, item_id)
            fts_cols = [
                "target_id",
                "kind",
                "item_id",
                "display_title",
                "title",
                "body",
                "published_at",
                "source_url",
            ]
            fts_rows = scon.execute(
                f"SELECT {','.join(fts_cols)} FROM search_fts"
            ).fetchall()
            if fts_rows:
                ph = ",".join("?" for _ in fts_cols)
                for r in fts_rows:
                    dcon.execute(
                        "DELETE FROM search_fts WHERE target_id = ? AND kind = ? AND item_id = ?",
                        (r[0], r[1], r[2]),
                    )
                    dcon.execute(
                        f"INSERT INTO search_fts({','.join(fts_cols)}) VALUES ({ph})",
                        r,
                    )
                counts.append(f"search_fts={len(fts_rows)}")

            dcon.commit()
            summary = ", ".join(counts) if counts else "0 rows"
            return f"Merged delta DB -> data/db/biliyt.db ({summary})"
        finally:
            dcon.close()
            scon.close()

    return f"Delta DB saved at {delta_rel}"


def verify_file(path: Path, expected_sha256: str) -> None:
    actual = sha256_file(path)
    if actual.lower() != expected_sha256.lower():
        raise RuntimeError(
            f"SHA-256 mismatch for {path.name}: expected {expected_sha256}, got {actual}"
        )


def restore_single_release(
    *,
    repo: str,
    tag: str,
    work_dir: Path,
    dest_dir: Path,
    preset: str,
    groups: list[str] | None,
    modules: list[str] | None,
    tiers: list[str] | None,
    online: bool,
    skip_verify: bool,
    list_only: bool,
) -> tuple[int, int]:
    base_url = (
        f"https://github.com/{repo}/releases/download/{tag}" if online else None
    )
    if online:
        work_dir.mkdir(parents=True, exist_ok=True)
        print(f"Fetching manifest for {repo}@{tag}...")
        for meta_name in ("MANIFEST.json", "SHA256SUMS.txt"):
            meta_path = work_dir / meta_name
            if not meta_path.is_file():
                download_file(f"{base_url}/{meta_name}", meta_path)

    manifest_path = work_dir / "MANIFEST.json"
    sums_path = work_dir / "SHA256SUMS.txt"

    if not manifest_path.is_file():
        raise FileNotFoundError(f"Missing MANIFEST.json in {work_dir}")

    checksums = parse_checksums(sums_path) if sums_path.is_file() else {}
    if not skip_verify and "MANIFEST.json" in checksums:
        verify_file(manifest_path, checksums["MANIFEST.json"])

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assets = select_assets(manifest, preset, groups, modules, tiers)
    if not assets:
        print(
            f"No assets matched in {tag} for preset='{preset}'"
            + (f", tiers={tiers}" if tiers else "")
            + ".",
            file=sys.stderr,
        )
        return 0, 0

    total_dl = sum(a["archive_bytes"] for a in assets)
    total_raw = sum(a["raw_bytes"] for a in assets)
    print(
        f"[{manifest.get('tag', tag)}] Matched {len(assets)} asset(s) for "
        f"preset='{preset}' (archive={format_size(total_dl)}, extracted={format_size(total_raw)}):"
    )
    for a in assets:
        print(
            f"  - {a['name']} [{a.get('tier', '-')}/{a.get('kind', '-')}] "
            f"({format_size(a['archive_bytes'])}, {a['files']:,} files)"
        )

    if list_only:
        return len(assets), 0

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

        if not skip_verify:
            if not expected_hash:
                raise RuntimeError(f"No SHA-256 checksum available for {name}")
            verify_file(arc_path, expected_hash)
            print("    [OK] SHA-256 verified")

        cnt, extracted_paths = safe_extract_tar_gz(arc_path, dest_dir)
        total_files += cnt
        print(f"    [OK] Extracted {cnt:,} file(s) -> {dest_dir}")

        if (
            manifest.get("release_type") == "delta"
            and asset.get("kind") == "sqlite-db"
        ):
            mod = str(asset.get("module") or "")
            for relp in extracted_paths:
                if relp.endswith(".db"):
                    msg = merge_delta_sqlite_db(relp, dest_dir, mod)
                    print(f"    [OK] {msg}")

    return len(assets), total_files


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download, verify, and restore weimingzi_archive release assets."
    )
    parser.add_argument(
        "--module",
        "--modules",
        dest="modules",
        nargs="+",
        choices=("all", "asr", "archive"),
        default=None,
        help="Pipeline module(s) to restore: all (default), asr, archive.",
    )
    parser.add_argument(
        "--tag",
        default="latest",
        help="Specific release tag (e.g. asr-v2026.07.17, archive-v2026.11.27) or 'latest'. Ignored if --src-dir is set.",
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
        choices=("light", "media", "raw", "full", "db"),
        default="light",
        help=(
            "light: core text/md + db (default); "
            "media: light + images/covers; "
            "raw: light + raw JSON & platform .srt / word-level JSON; "
            "full: all assets; "
            "db: SQLite database(s) only"
        ),
    )
    parser.add_argument(
        "--tiers",
        nargs="+",
        choices=("light", "images", "raw", "full"),
        default=None,
        help="Optional explicit tier filter (overrides --preset tier selection, e.g. --tiers images).",
    )
    parser.add_argument(
        "--groups",
        nargs="+",
        help="Optional group filter, e.g. bili_23191782 yt_wujixuanshi",
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
        "--no-deltas",
        action="store_true",
        help="In '--tag latest' mode, restore only base full snapshots and skip incremental delta releases.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Only list matching assets and sizes without downloading/extracting archives.",
    )

    args = parser.parse_args()
    dest_dir: Path = args.dest
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Normalize requested modules
    if args.modules is None or "all" in args.modules:
        requested_modules = list(KNOWN_MODULES)
        explicit_module_filter: list[str] | None = None
    else:
        requested_modules = [m for m in KNOWN_MODULES if m in args.modules]
        explicit_module_filter = requested_modules

    tmp_ctx = None
    try:
        if args.src_dir is not None:
            if not args.src_dir.is_dir():
                raise FileNotFoundError(f"Source directory not found: {args.src_dir}")
            arc_cnt, file_cnt = restore_single_release(
                repo=args.repo,
                tag=args.tag,
                work_dir=args.src_dir,
                dest_dir=dest_dir,
                preset=args.preset,
                groups=args.groups,
                modules=explicit_module_filter,
                tiers=args.tiers,
                online=False,
                skip_verify=args.skip_verify,
                list_only=args.list,
            )
            if not args.list and arc_cnt > 0:
                print(
                    f"Restore complete: {arc_cnt} archive(s), {file_cnt:,} file(s) "
                    f"restored under {dest_dir / 'data'}"
                )
            return 0 if arc_cnt > 0 else 1

        # Online mode: resolve release tag(s)
        if args.tag == "latest":
            targets = resolve_latest_tags_by_module(
                args.repo,
                requested_modules,
                include_deltas=not args.no_deltas,
            )
        else:
            targets = [(None, args.tag)]

        if args.cache_dir is not None:
            base_cache = args.cache_dir
            base_cache.mkdir(parents=True, exist_ok=True)
        else:
            tmp_ctx = tempfile.TemporaryDirectory(prefix="wmz_restore_")
            base_cache = Path(tmp_ctx.name)

        total_archives = 0
        total_files = 0
        for mod_hint, tag in targets:
            work_dir = base_cache / tag
            mod_filter = [mod_hint] if mod_hint else explicit_module_filter
            arc_cnt, file_cnt = restore_single_release(
                repo=args.repo,
                tag=tag,
                work_dir=work_dir,
                dest_dir=dest_dir,
                preset=args.preset,
                groups=args.groups,
                modules=mod_filter,
                tiers=args.tiers,
                online=True,
                skip_verify=args.skip_verify,
                list_only=args.list,
            )
            total_archives += arc_cnt
            total_files += file_cnt

        if not args.list:
            if total_archives == 0:
                return 1
            print(
                f"Restore complete: {total_archives} archive(s), {total_files:,} file(s) "
                f"restored under {dest_dir / 'data'}"
            )
        return 0
    except urllib.error.HTTPError as e:
        print(
            f"HTTP error {e.code} while accessing GitHub Releases: {e.url}",
            file=sys.stderr,
        )
        return 1
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
