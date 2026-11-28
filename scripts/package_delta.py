#!/usr/bin/env python3
"""Automated immutable time-range delta packager & publisher for weimingzi_archive.

Pure Python 3.9+ standard library implementation.
Designed for unattended weekly (or on-demand) execution on ECS or local hosts:

1. Uses `catalog/*.csv` in the git repository as the single source of truth for
   already-published items (no drifting cursor files).
2. Enforces a hardcoded creator whitelist (`bili_23191782`, `yt_wujixuanshi`).
3. Takes read-only SQLite backups of upstream `pipeline.db` and `biliyt.db`
   (optionally waiting for upstream lock files first).
4. Packages immutable time-range delta releases:
   - `asr-delta-YYYY.MM.DD-YYYY.MM.DD[-rN]`
   - `archive-delta-YYYY.MM.DD-YYYY.MM.DD[-rN]`
   with standalone sanitized delta DBs under `data/db/deltas/*.db` so base DBs
   are never overwritten and `scripts/restore.py` merges them automatically.
5. Enforces fail-closed sanitization checks + offline `restore.py` smoke tests
   before touching git or GitHub.
6. Publishes atomically: upload Release assets as `--draft` -> commit & push
   `main` -> undraft Release.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "chosenecho/weimingzi_archive"
DEFAULT_ASR_ROOT = Path("/opt/qwen3-asr-pipeline")
DEFAULT_BILIYT_ROOT = Path("/opt/biliyt")
DEFAULT_MAX_PART_RAW_MB = 450
GITHUB_ASSET_LIMIT_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

# Hardcoded creator whitelist — NEVER derive dynamically from upstream configs!
ALLOWED_GROUPS: tuple[str, ...] = ("bili_23191782", "yt_wujixuanshi")
GROUP_PLATFORMS: dict[str, str] = {
    "bili_23191782": "bilibili",
    "yt_wujixuanshi": "youtube",
}

EXCLUDED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".mkv",
        ".webm",
        ".flv",
        ".mov",
        ".avi",
        ".m4a",
        ".mp3",
        ".wav",
        ".flac",
        ".ogg",
        ".part",
        ".ytdl",
        ".tmp",
    }
)
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif"}
)
FORBIDDEN_TOKENS: tuple[str, ...] = (
    "/opt/",
    "C:\\",
    "D:\\",
    "c:\\",
    "d:\\",
    "ChosenEcho",
    "SESSDATA",
    "bili_jct",
    "buvid3",
    "modal.run",
    "127.0.0.1",
    "localhost:",
    "182.92.",
    "ghp_",
    "github_pat_",
)

DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*-\s*")
BILI_PART_RE = re.compile(r"^(BV[0-9A-Za-z]+)_p(\d+)$")
README_TABLE_START = "<!-- AUTO_SNAPSHOT_TABLE_START -->"
README_TABLE_END = "<!-- AUTO_SNAPSHOT_TABLE_END -->"

BILIYT_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL,
        description TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profiles (
        target_id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        uid TEXT,
        name TEXT,
        sign TEXT,
        level INTEGER,
        official_title TEXT,
        follower INTEGER,
        following INTEGER,
        archive_views INTEGER,
        likes INTEGER,
        avatar_relpath TEXT,
        banner_relpath TEXT,
        collections_json TEXT,
        raw_json TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profile_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        follower INTEGER,
        following INTEGER,
        archive_views INTEGER,
        likes INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        video_id TEXT NOT NULL,
        aid TEXT,
        title TEXT,
        description TEXT,
        uploader TEXT,
        upload_date TEXT,
        duration REAL,
        view_count INTEGER,
        like_count INTEGER,
        comment_count INTEGER,
        webpage_url TEXT,
        subtitle_text TEXT,
        video_relpath TEXT,
        info_json_relpath TEXT,
        subtitle_relpath TEXT,
        thumbnail_relpath TEXT,
        raw_json TEXT,
        archived_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(target_id, video_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dynamics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT NOT NULL,
        dynamic_id TEXT NOT NULL,
        dynamic_type TEXT,
        published_at TEXT,
        pub_ts INTEGER,
        author_name TEXT,
        text_content TEXT,
        markdown_content TEXT,
        is_forward INTEGER NOT NULL DEFAULT 0,
        orig_dynamic_id TEXT,
        orig_text TEXT,
        like_count INTEGER,
        comment_count INTEGER,
        forward_count INTEGER,
        comment_oid TEXT,
        comment_type INTEGER,
        images_json TEXT,
        folder_relpath TEXT,
        raw_json TEXT,
        archived_at TEXT NOT NULL,
        UNIQUE(target_id, dynamic_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT NOT NULL,
        cv_id TEXT NOT NULL,
        opus_id TEXT,
        title TEXT,
        category TEXT,
        published_at TEXT,
        pub_ts INTEGER,
        summary TEXT,
        text_content TEXT,
        markdown_content TEXT,
        words INTEGER,
        view_count INTEGER,
        like_count INTEGER,
        reply_count INTEGER,
        images_json TEXT,
        folder_relpath TEXT,
        raw_json TEXT,
        archived_at TEXT NOT NULL,
        UNIQUE(target_id, cv_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT NOT NULL,
        carrier_kind TEXT NOT NULL,
        oid TEXT NOT NULL,
        rtype INTEGER NOT NULL,
        rpid TEXT NOT NULL,
        root_rpid TEXT,
        author_mid TEXT,
        author_name TEXT,
        is_up INTEGER NOT NULL DEFAULT 0,
        is_pinned INTEGER NOT NULL DEFAULT 0,
        has_up_reply INTEGER NOT NULL DEFAULT 0,
        content TEXT,
        like_count INTEGER,
        ctime INTEGER,
        published_at TEXT,
        fetched_at TEXT NOT NULL,
        UNIQUE(target_id, carrier_kind, oid, rpid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS favorites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT NOT NULL,
        media_id TEXT NOT NULL,
        title TEXT,
        media_count INTEGER,
        updated_at TEXT NOT NULL,
        UNIQUE(target_id, media_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS favorite_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT NOT NULL,
        media_id TEXT NOT NULL,
        bvid TEXT NOT NULL,
        title TEXT,
        upper_name TEXT,
        duration INTEGER,
        fav_time TEXT,
        url TEXT,
        UNIQUE(target_id, media_id, bvid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mode TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        summary_json TEXT NOT NULL
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
        target_id UNINDEXED,
        kind UNINDEXED,
        item_id UNINDEXED,
        display_title UNINDEXED,
        title,
        body,
        published_at UNINDEXED,
        source_url UNINDEXED,
        tokenize = 'trigram'
    )
    """,
)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


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


def clean_preview(text: str | None, max_len: int = 120) -> str:
    if not text:
        return ""
    s = " ".join(text.split())
    return s[:max_len] + ("..." if len(s) > max_len else "")


def make_source_url(group: str, video_id: str) -> str:
    if group == "bili_23191782":
        m = BILI_PART_RE.match(video_id)
        if m:
            return f"https://www.bilibili.com/video/{m.group(1)}?p={m.group(2)}"
        return f"https://www.bilibili.com/video/{video_id}"
    if group == "yt_wujixuanshi":
        return f"https://www.youtube.com/watch?v={video_id}"
    return ""


def extract_upload_date(video_rel_path: str | None, title: str | None) -> str:
    for text in (video_rel_path or "", title or ""):
        m = DATE_PREFIX_RE.match(Path(text.replace("\\", "/")).name)
        if m:
            return m.group(1)
    return ""


def backup_sqlite_readonly(src_db: Path, dst_db: Path) -> None:
    """Create a consistent WAL-merged read-only snapshot of a live SQLite DB."""
    src_con = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True, timeout=30.0)
    dst_con = sqlite3.connect(str(dst_db))
    try:
        src_con.backup(dst_con)
        dst_con.commit()
    finally:
        dst_con.close()
        src_con.close()


def wait_for_upstream_locks(lock_paths: list[Path], timeout_sec: int) -> None:
    """Wait politely if upstream sync/ASR flock files are currently held."""
    if fcntl is None or not lock_paths:
        return
    deadline = time.time() + max(timeout_sec, 0)
    for lp in lock_paths:
        if not lp.exists():
            continue
        while True:
            try:
                with lp.open("r+") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise TimeoutError(
                        f"Timed out waiting for upstream lock {lp} after {timeout_sec}s"
                    )
                log(f"Upstream lock {lp} is busy; waiting 10s...")
                time.sleep(10)
            except OSError:
                break


def split_entries_by_raw_size(
    entries: list[tuple[Path, str]], max_part_raw_bytes: int
) -> list[list[tuple[Path, str]]]:
    if not entries:
        return []
    if max_part_raw_bytes <= 0:
        return [entries]
    parts: list[list[tuple[Path, str]]] = []
    cur: list[tuple[Path, str]] = []
    cur_bytes = 0
    for src, arcname in entries:
        sz = src.stat().st_size
        if cur and cur_bytes + sz > max_part_raw_bytes:
            parts.append(cur)
            cur = []
            cur_bytes = 0
        cur.append((src, arcname))
        cur_bytes += sz
    if cur:
        parts.append(cur)
    return parts


def build_tar_gz(
    archive_path: Path,
    entries: Iterable[tuple[Path, str]],
    compresslevel: int = 6,
) -> tuple[int, int]:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
    count = 0
    raw_bytes = 0
    try:
        with tarfile.open(
            tmp_path,
            mode="w:gz",
            format=tarfile.PAX_FORMAT,
            encoding="utf-8",
            compresslevel=compresslevel,
        ) as tf:
            for src, arcname in entries:
                if src.suffix.lower() in EXCLUDED_EXTENSIONS:
                    raise RuntimeError(f"Refusing to pack excluded media binary: {src}")
                if src.name.lower() in {"cookies.txt", "config.yaml"}:
                    raise RuntimeError(f"Refusing to pack sensitive file: {src}")
                arcname = arcname.replace("\\", "/").lstrip("/")
                tf.add(src, arcname=arcname, recursive=False)
                count += 1
                raw_bytes += src.stat().st_size
        tmp_path.replace(archive_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return count, raw_bytes


def verify_text_file_sanitized(path: Path) -> None:
    if path.suffix.lower() in IMAGE_EXTENSIONS or path.suffix.lower() in (".db", ".gz"):
        return
    text = path.read_text(encoding="utf-8", errors="ignore")
    for tok in FORBIDDEN_TOKENS:
        if tok in text:
            idx = text.find(tok)
            snippet = text[max(0, idx - 30) : idx + 80].replace("\n", " ")
            raise RuntimeError(
                f"Sanitization check FAILED in {path}: found '{tok}' near '{snippet}'"
            )


def verify_db_sanitized(
    db_path: Path, tables: Iterable[str], group_col_by_table: dict[str, str]
) -> None:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        existing = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        allowed = set(ALLOWED_GROUPS)
        for tbl in tables:
            if tbl not in existing:
                continue
            gcol = group_col_by_table.get(tbl)
            if gcol:
                found = {
                    r[0]
                    for r in con.execute(
                        f'SELECT DISTINCT "{gcol}" FROM "{tbl}" WHERE "{gcol}" IS NOT NULL'
                    ).fetchall()
                }
                if not found.issubset(allowed):
                    raise RuntimeError(
                        f"Whitelist check FAILED in {db_path.name}:{tbl}: {found - allowed}"
                    )
            for row in con.execute(f'SELECT * FROM "{tbl}"').fetchall():
                for val in row:
                    if isinstance(val, str):
                        for tok in FORBIDDEN_TOKENS:
                            if tok in val:
                                raise RuntimeError(
                                    f"Sanitization check FAILED in {db_path.name}:{tbl}: "
                                    f"found '{tok}' in '{val[:120]}'"
                                )
    finally:
        con.close()


def read_catalog_ids(csv_path: Path, key_col: str) -> set[str]:
    if not csv_path.is_file():
        return set()
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        return {row[key_col] for row in csv.DictReader(f) if row.get(key_col)}


def pick_unique_delta_tag(
    prefix: str,
    start_date: str,
    end_date: str,
    repo_root: Path,
    existing_remote_tags: set[str],
) -> str:
    """Return an immutable delta tag, appending -r2/-r3 if the date range already exists."""
    base = f"{prefix}-delta-{start_date.replace('-', '.')}-{end_date.replace('-', '.')}"
    manifests_dir = repo_root / "manifests"
    candidate = base
    rev = 1
    while (manifests_dir / candidate).exists() or candidate in existing_remote_tags:
        rev += 1
        candidate = f"{base}-r{rev}"
    return candidate


def list_remote_release_tags(repo: str) -> set[str]:
    if not shutil.which("gh"):
        return set()
    proc = subprocess.run(
        ["gh", "release", "list", "-R", repo, "--limit", "200", "--json", "tagName"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return set()
    try:
        items = json.loads(proc.stdout)
        return {str(x["tagName"]) for x in items if isinstance(x, dict) and x.get("tagName")}
    except Exception:
        return set()


def run_cmd_with_retry(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    retries: int = 4,
    label: str = "",
) -> None:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)
            return
        except subprocess.CalledProcessError as exc:
            last_err = exc
            if attempt < retries:
                wait = 3 * attempt
                log(
                    f"[WARN] {label or cmd[0]} failed (attempt {attempt}/{retries}); "
                    f"retrying in {wait}s..."
                )
                time.sleep(wait)
    assert last_err is not None
    raise last_err


# ---------------------------------------------------------------------------
# ASR Delta Detection & Packaging
# ---------------------------------------------------------------------------


def detect_asr_delta(
    asr_root: Path, repo_root: Path, tmp_dir: Path
) -> dict | None:
    src_db = asr_root / "data" / "pipeline.db"
    transcripts_dir = asr_root / "data" / "transcripts"
    if not src_db.is_file() or not transcripts_dir.is_dir():
        log(f"[asr] Upstream data not found under {asr_root}; skipping.")
        return None

    snap_db = tmp_dir / "asr_snapshot.db"
    backup_sqlite_readonly(src_db, snap_db)

    published_by_group = {
        grp: read_catalog_ids(repo_root / "catalog" / f"asr_{grp}.csv", "video_id")
        for grp in ALLOWED_GROUPS
    }

    new_items: list[dict] = []
    con = sqlite3.connect(f"file:{snap_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        ph = ",".join("?" for _ in ALLOWED_GROUPS)
        rows = con.execute(
            f"""
            SELECT id, source_group, video_id, title, video_rel_path, duration_sec,
                   txt_path, srt_path, json_path, asr_status, asr_finished_at
            FROM media_items
            WHERE source_group IN ({ph}) AND asr_status = 'done'
            ORDER BY source_group ASC, video_rel_path ASC, video_id ASC
            """,
            ALLOWED_GROUPS,
        ).fetchall()

        for r in rows:
            grp = str(r["source_group"])
            vid = str(r["video_id"])
            if vid in published_by_group[grp]:
                continue
            gdir = transcripts_dir / grp
            txt_name = Path(str(r["txt_path"] or "").replace("\\", "/")).name
            srt_name = Path(str(r["srt_path"] or "").replace("\\", "/")).name
            json_name = Path(str(r["json_path"] or "").replace("\\", "/")).name
            txt_file = gdir / txt_name if txt_name else None
            srt_file = gdir / srt_name if srt_name else None
            json_file = gdir / json_name if json_name else None

            if not (
                txt_file
                and txt_file.is_file()
                and txt_file.stat().st_size > 0
                and srt_file
                and srt_file.is_file()
                and srt_file.stat().st_size > 0
            ):
                log(
                    f"[asr] Skipping incomplete item {grp}/{vid}: missing non-empty .txt or .srt"
                )
                continue
            if json_name and not (
                json_file and json_file.is_file() and json_file.stat().st_size > 0
            ):
                log(
                    f"[asr] Skipping incomplete item {grp}/{vid}: missing non-empty .json"
                )
                continue

            upload_date = extract_upload_date(r["video_rel_path"], r["title"])
            if not upload_date:
                upload_date = (r["asr_finished_at"] or "")[:10] or datetime.now(
                    timezone.utc
                ).strftime("%Y-%m-%d")

            new_items.append(
                {
                    "row_id": int(r["id"]),
                    "group": grp,
                    "video_id": vid,
                    "title": str(r["title"] or ""),
                    "video_rel_path": str(r["video_rel_path"] or ""),
                    "upload_date": upload_date,
                    "duration_sec": float(r["duration_sec"])
                    if r["duration_sec"] is not None
                    else 0.0,
                    "asr_status": str(r["asr_status"]),
                    "asr_finished_at": str(r["asr_finished_at"] or ""),
                    "txt_file": txt_file,
                    "srt_file": srt_file,
                    "json_file": json_file if (json_file and json_file.is_file()) else None,
                }
            )
    finally:
        con.close()

    if not new_items:
        return None

    dates = sorted(x["upload_date"] for x in new_items if x["upload_date"])
    return {
        "snapshot_db": snap_db,
        "items": new_items,
        "groups": sorted({x["group"] for x in new_items}),
        "date_start": dates[0],
        "date_end": dates[-1],
    }


def package_asr_delta(
    delta: dict,
    *,
    tag: str,
    base_tag: str,
    repo: str,
    out_dir: Path,
    max_part_raw_mb: int,
    compresslevel: int,
) -> dict:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    items: list[dict] = delta["items"]
    groups: list[str] = delta["groups"]
    date_start: str = delta["date_start"]
    date_end: str = delta["date_end"]
    max_raw_bytes = max_part_raw_mb * 1024 * 1024

    assets: list[dict] = []
    added_bytes_by_group: dict[str, dict[str, int]] = defaultdict(
        lambda: {"txt": 0, "srt": 0, "json": 0}
    )

    for grp in groups:
        g_items = [x for x in items if x["group"] == grp]
        text_entries: list[tuple[Path, str]] = []
        json_entries: list[tuple[Path, str]] = []
        seen_arc: set[str] = set()

        for it in g_items:
            for key, fpath in (("txt", it["txt_file"]), ("srt", it["srt_file"])):
                verify_text_file_sanitized(fpath)
                arc = f"data/transcripts/{grp}/{fpath.name}"
                if arc not in seen_arc:
                    seen_arc.add(arc)
                    text_entries.append((fpath, arc))
                    added_bytes_by_group[grp][key] += fpath.stat().st_size
            if it["json_file"] is not None:
                fpath = it["json_file"]
                verify_text_file_sanitized(fpath)
                arc = f"data/transcripts/{grp}/{fpath.name}"
                if arc not in seen_arc:
                    seen_arc.add(arc)
                    json_entries.append((fpath, arc))
                    added_bytes_by_group[grp]["json"] += fpath.stat().st_size

        text_parts = split_entries_by_raw_size(text_entries, max_raw_bytes)
        for idx, part in enumerate(text_parts, 1):
            suffix = "" if len(text_parts) == 1 else f"-part{idx:02d}"
            arc_path = out_dir / f"{tag}-{grp}-text{suffix}.tar.gz"
            fcnt, rbytes = build_tar_gz(arc_path, part, compresslevel=compresslevel)
            assets.append(
                {
                    "name": arc_path.name,
                    "module": "asr",
                    "group": grp,
                    "tier": "light",
                    "kind": "text+srt",
                    "files": fcnt,
                    "raw_bytes": rbytes,
                    "archive_bytes": arc_path.stat().st_size,
                    "sha256": sha256_file(arc_path),
                }
            )

        json_parts = split_entries_by_raw_size(json_entries, max_raw_bytes)
        for idx, part in enumerate(json_parts, 1):
            suffix = "" if len(json_parts) == 1 else f"-part{idx:02d}"
            arc_path = out_dir / f"{tag}-{grp}-json{suffix}.tar.gz"
            fcnt, rbytes = build_tar_gz(arc_path, part, compresslevel=compresslevel)
            assets.append(
                {
                    "name": arc_path.name,
                    "module": "asr",
                    "group": grp,
                    "tier": "full",
                    "kind": "word-json",
                    "files": fcnt,
                    "raw_bytes": rbytes,
                    "archive_bytes": arc_path.stat().st_size,
                    "sha256": sha256_file(arc_path),
                }
            )

    csv_rows_by_group: dict[str, list[dict]] = defaultdict(list)
    with tempfile.TemporaryDirectory(prefix="wmz_asr_db_") as td:
        db_suffix = tag.removeprefix("asr-delta-").replace(".", "-")
        delta_db = Path(td) / f"asr_pipeline-{db_suffix}.db"
        scon = sqlite3.connect(f"file:{delta['snapshot_db']}?mode=ro", uri=True)
        dcon = sqlite3.connect(str(delta_db))
        try:
            scon.backup(dcon)
            dcon.commit()
            keep_ids = [it["row_id"] for it in items]
            ph = ",".join("?" for _ in keep_ids)
            dcon.execute(f"DELETE FROM media_items WHERE id NOT IN ({ph})", keep_ids)
            dcon.execute("DELETE FROM pipeline_runs")

            updates = []
            for it in items:
                grp = it["group"]
                vname = Path(it["video_rel_path"].replace("\\", "/")).name
                norm_video = f"videos/{grp}/{vname}"
                norm_txt = f"data/transcripts/{grp}/{it['txt_file'].name}"
                norm_srt = f"data/transcripts/{grp}/{it['srt_file'].name}"
                norm_json = (
                    f"data/transcripts/{grp}/{it['json_file'].name}"
                    if it["json_file"]
                    else None
                )
                updates.append(
                    (norm_video, vname, norm_txt, norm_srt, norm_json, it["row_id"])
                )
                csv_rows_by_group[grp].append(
                    {
                        "video_id": it["video_id"],
                        "upload_date": it["upload_date"],
                        "title": it["title"],
                        "duration_sec": round(it["duration_sec"], 2),
                        "source_url": make_source_url(grp, it["video_id"]),
                        "txt_rel_path": norm_txt,
                        "srt_rel_path": norm_srt,
                        "json_rel_path": norm_json or "",
                        "asr_status": it["asr_status"],
                        "asr_finished_at": it["asr_finished_at"],
                    }
                )

            dcon.executemany(
                "UPDATE media_items "
                "SET video_path = ?, video_rel_path = ?, audio_path = NULL, "
                "    asr_server_url = NULL, asr_task_id = NULL, "
                "    txt_path = ?, srt_path = ?, json_path = ? "
                "WHERE id = ?",
                updates,
            )
            dcon.commit()
            dcon.execute("PRAGMA journal_mode = DELETE")
            dcon.execute("VACUUM")
        finally:
            dcon.close()
            scon.close()

        verify_db_sanitized(
            delta_db,
            ("media_items", "pipeline_runs"),
            {"media_items": "source_group", "pipeline_runs": "source_group"},
        )

        db_arc = out_dir / f"{tag}-pipeline-db.tar.gz"
        fcnt, rbytes = build_tar_gz(
            db_arc, [(delta_db, f"data/db/deltas/{delta_db.name}")], compresslevel=compresslevel
        )
        assets.append(
            {
                "name": db_arc.name,
                "module": "asr",
                "group": "_meta",
                "tier": "light",
                "kind": "sqlite-db",
                "files": fcnt,
                "raw_bytes": rbytes,
                "archive_bytes": db_arc.stat().st_size,
                "sha256": sha256_file(db_arc),
            }
        )

    oversized = [a for a in assets if a["archive_bytes"] >= GITHUB_ASSET_LIMIT_BYTES]
    if oversized:
        raise RuntimeError(f"ASR delta assets exceed 2 GiB cap: {oversized}")

    manifest = {
        "schema_version": 1,
        "release_type": "delta",
        "base_tag": base_tag,
        "date_range": {"start": date_start, "end": date_end},
        "tag": tag,
        "repo": repo,
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "modules": ["asr"],
        "groups": groups,
        "total_archive_bytes": sum(a["archive_bytes"] for a in assets),
        "total_raw_bytes": sum(a["raw_bytes"] for a in assets),
        "assets": assets,
    }
    manifest_path = out_dir / "MANIFEST.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    sums = [f"{a['sha256']}  {a['name']}" for a in assets]
    sums.append(f"{sha256_file(manifest_path)}  {manifest_path.name}")
    (out_dir / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")

    total_hours = sum(it["duration_sec"] for it in items) / 3600.0
    light_bytes = sum(a["archive_bytes"] for a in assets if a["tier"] == "light")
    full_bytes = manifest["total_archive_bytes"]
    notes = [
        f"## ASR 增量转录包 `{tag}`（{date_start} ~ {date_end}）",
        "",
        f"本 Release 为基线快照 [`{base_tag}`](https://github.com/{repo}/releases/tag/{base_tag}) 之后的**不可变时间段增量包**，覆盖 **{date_start} ~ {date_end}** 期间新增的 **{len(items)} 条有声视频**（共 **{total_hours:.2f} 小时**）的 Qwen3-ASR 语音转录产物与脱敏增量库。",
        "",
        "### 本期新增视频明细",
        "",
        "| 日期 | 分组 | 视频 ID | 标题 | 时长（分钟） |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for it in items:
        url = make_source_url(it["group"], it["video_id"])
        notes.append(
            f"| {it['upload_date']} | `{it['group']}` | [`{it['video_id']}`]({url}) | "
            f"{it['title']} | {it['duration_sec'] / 60.0:.1f} |"
        )
    notes.extend(
        [
            "",
            "### 快速下载与还原",
            "",
            "```bash",
            "# 一键还原最新基线 + 全部增量（自动合并 SQLite）",
            "python scripts/restore.py --modules asr --preset light",
            "",
            "# 仅拉取本期增量并合入已有 data/",
            f"python scripts/restore.py --tag {tag} --preset light  # {format_size(light_bytes)}",
            f"python scripts/restore.py --tag {tag} --preset full   # {format_size(full_bytes)}",
            "```",
            "",
            "### 资产清单",
            "",
            "| Asset | Group | Tier | Type | Files | Raw Size | Archive Size |",
            "|---|---|---|---|---:|---:|---:|",
        ]
    )
    for a in assets:
        notes.append(
            f"| `{a['name']}` | `{a['group']}` | `{a['tier']}` | {a['kind']} | "
            f"{a['files']:,} | {format_size(a['raw_bytes'])} | {format_size(a['archive_bytes'])} |"
        )
    notes.extend(
        [
            "",
            f"**总计**：{len(assets)} 个归档分包 | 原始体积 {format_size(manifest['total_raw_bytes'])} → 压缩体积 {format_size(full_bytes)}",
            "",
            "### 完整性校验",
            "",
            "```bash",
            "sha256sum -c SHA256SUMS.txt",
            "```",
        ]
    )
    (out_dir / "RELEASE_NOTES.md").write_text("\n".join(notes) + "\n", encoding="utf-8")

    return {
        "module": "asr",
        "tag": tag,
        "title": f"ASR Transcripts Delta {date_start} ~ {date_end} ({tag})",
        "out_dir": out_dir,
        "manifest": manifest,
        "date_start": date_start,
        "date_end": date_end,
        "item_count": len(items),
        "duration_hours": round(total_hours, 2),
        "csv_rows_by_group": dict(csv_rows_by_group),
        "added_bytes_by_group": dict(added_bytes_by_group),
    }


# ---------------------------------------------------------------------------
# Archive Delta Detection & Packaging
# ---------------------------------------------------------------------------


def detect_archive_delta(
    biliyt_root: Path, repo_root: Path, tmp_dir: Path
) -> dict | None:
    src_db = biliyt_root / "state" / "biliyt.db"
    dl_root = biliyt_root / "downloads"
    if not src_db.is_file() or not dl_root.is_dir():
        log(f"[archive] Upstream data not found under {biliyt_root}; skipping.")
        return None

    snap_db = tmp_dir / "biliyt_snapshot.db"
    backup_sqlite_readonly(src_db, snap_db)

    pub_videos = {
        grp: read_catalog_ids(repo_root / "catalog" / f"videos_{grp}.csv", "video_id")
        for grp in ALLOWED_GROUPS
    }
    pub_dynamics = {
        grp: read_catalog_ids(repo_root / "catalog" / f"dynamics_{grp}.csv", "dynamic_id")
        for grp in ALLOWED_GROUPS
    }
    pub_articles = {
        grp: read_catalog_ids(repo_root / "catalog" / f"articles_{grp}.csv", "cv_id")
        for grp in ALLOWED_GROUPS
    }

    con = sqlite3.connect(f"file:{snap_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    new_videos: list[dict] = []
    new_dynamics: list[dict] = []
    new_articles: list[dict] = []
    dates: list[str] = []

    try:
        ph = ",".join("?" for _ in ALLOWED_GROUPS)

        for r in con.execute(
            f"SELECT * FROM videos WHERE target_id IN ({ph}) ORDER BY upload_date ASC, video_id ASC",
            ALLOWED_GROUPS,
        ):
            grp = str(r["target_id"])
            vid = str(r["video_id"])
            if vid in pub_videos[grp]:
                continue
            gdir = dl_root / GROUP_PLATFORMS[grp] / grp
            if not gdir.is_dir():
                continue
            matched = [
                p
                for p in sorted(gdir.iterdir())
                if p.is_file()
                and p.suffix.lower() not in EXCLUDED_EXTENSIONS
                and not p.name.startswith(".")
                and f"[{vid}]" in p.name
            ]
            if not any(p.name.lower().endswith(".info.json") for p in matched):
                log(f"[archive] Skipping video {grp}/{vid}: .info.json not on disk")
                continue
            ud = str(r["upload_date"] or "")
            if len(ud) == 10 and ud != "unknown":
                dates.append(ud)
            new_videos.append({"row": dict(r), "group": grp, "files": matched})

        for r in con.execute(
            f"SELECT * FROM dynamics WHERE target_id IN ({ph}) ORDER BY published_at ASC, dynamic_id ASC",
            ALLOWED_GROUPS,
        ):
            grp = str(r["target_id"])
            did = str(r["dynamic_id"])
            if did in pub_dynamics[grp]:
                continue
            gdir = dl_root / GROUP_PLATFORMS[grp] / grp
            folder_rel = str(r["folder_relpath"] or "").replace("\\", "/").lstrip("/")
            fdir = gdir / folder_rel if folder_rel else None
            if not (fdir and fdir.is_dir()):
                dyn_root = gdir / "dynamics"
                candidates = list(dyn_root.glob(f"*{did}")) if dyn_root.is_dir() else []
                fdir = candidates[0] if candidates else None
            if not (fdir and fdir.is_dir()):
                log(f"[archive] Skipping dynamic {grp}/{did}: folder not on disk")
                continue
            matched = [
                p
                for p in sorted(fdir.rglob("*"))
                if p.is_file()
                and p.suffix.lower() not in EXCLUDED_EXTENSIONS
                and not p.name.startswith(".")
            ]
            if not matched:
                continue
            pd = str(r["published_at"] or "")[:10]
            if len(pd) == 10:
                dates.append(pd)
            new_dynamics.append({"row": dict(r), "group": grp, "files": matched})

        for r in con.execute(
            f"SELECT * FROM articles WHERE target_id IN ({ph}) ORDER BY published_at ASC, cv_id ASC",
            ALLOWED_GROUPS,
        ):
            grp = str(r["target_id"])
            cv = str(r["cv_id"])
            if cv in pub_articles[grp]:
                continue
            gdir = dl_root / GROUP_PLATFORMS[grp] / grp
            folder_rel = str(r["folder_relpath"] or "").replace("\\", "/").lstrip("/")
            fdir = gdir / folder_rel if folder_rel else None
            if not (fdir and fdir.is_dir()):
                art_root = gdir / "articles"
                candidates = list(art_root.glob(f"*cv{cv}*")) if art_root.is_dir() else []
                fdir = candidates[0] if candidates else None
            if not (fdir and fdir.is_dir()):
                log(f"[archive] Skipping article {grp}/{cv}: folder not on disk")
                continue
            matched = [
                p
                for p in sorted(fdir.rglob("*"))
                if p.is_file()
                and p.suffix.lower() not in EXCLUDED_EXTENSIONS
                and not p.name.startswith(".")
            ]
            if not matched:
                continue
            pd = str(r["published_at"] or "")[:10]
            if len(pd) == 10:
                dates.append(pd)
            new_articles.append({"row": dict(r), "group": grp, "files": matched})
    finally:
        con.close()

    if not (new_videos or new_dynamics or new_articles):
        return None

    groups = sorted(
        {x["group"] for x in new_videos}
        | {x["group"] for x in new_dynamics}
        | {x["group"] for x in new_articles}
    )
    if not dates:
        dates = [datetime.now(timezone.utc).strftime("%Y-%m-%d")]
    dates.sort()
    return {
        "snapshot_db": snap_db,
        "dl_root": dl_root,
        "videos": new_videos,
        "dynamics": new_dynamics,
        "articles": new_articles,
        "groups": groups,
        "date_start": dates[0],
        "date_end": dates[-1],
    }


def classify_archive_file(group: str, rel: str) -> str:
    parts = rel.split("/")
    name_lower = parts[-1].lower()
    ext = Path(name_lower).suffix

    if group == "bili_23191782":
        if parts[0] == "profile":
            return "md"
        if parts[0] in ("dynamics", "articles"):
            if "images" in parts[1:-1] or ext in IMAGE_EXTENSIONS:
                return "images"
            if name_lower == "comments.json":
                return "comments-json"
            if name_lower in ("dynamic.json", "article.json"):
                return "meta-json"
            if name_lower in ("dynamic.md", "article.md", "comments.md"):
                return "md"
            raise RuntimeError(f"Unclassified file in {group}/{parts[0]}: {rel}")
        if len(parts) == 1:
            if name_lower.endswith(".comments.md"):
                return "md"
            if name_lower.endswith(".comments.json"):
                return "comments-json"
            if name_lower.endswith(".info.json"):
                return "meta-json"
            if ext == ".srt":
                return "platform-srt"
            if ext in IMAGE_EXTENSIONS:
                return "images"
            raise RuntimeError(f"Unclassified root file in {group}: {rel}")
        raise RuntimeError(f"Unexpected subdirectory in {group}: {rel}")

    if group == "yt_wujixuanshi":
        if name_lower.endswith(".md"):
            return "md"
        if ext in IMAGE_EXTENSIONS:
            return "images"
        if (
            name_lower.endswith(".info.json")
            or ext == ".srt"
            or name_lower.endswith(".comments.json")
        ):
            return "raw"
        raise RuntimeError(f"Unclassified file in {group}: {rel}")

    raise ValueError(f"Unsupported group: {group}")


def package_archive_delta(
    delta: dict,
    *,
    tag: str,
    base_tag: str,
    repo: str,
    out_dir: Path,
    max_part_raw_mb: int,
    compresslevel: int,
) -> dict:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    dl_root: Path = delta["dl_root"]
    groups: list[str] = delta["groups"]
    date_start: str = delta["date_start"]
    date_end: str = delta["date_end"]
    max_raw_bytes = max_part_raw_mb * 1024 * 1024

    buckets_by_group: dict[str, dict[str, list[tuple[Path, str]]]] = {
        g: defaultdict(list) for g in groups
    }
    seen_files: dict[str, set[str]] = {g: set() for g in groups}

    for entry_list in (delta["videos"], delta["dynamics"], delta["articles"]):
        for item in entry_list:
            grp = item["group"]
            platform = GROUP_PLATFORMS[grp]
            groot = dl_root / platform / grp
            prefix = f"data/archive/{platform}/{grp}"
            for fp in item["files"]:
                rel = fp.relative_to(groot).as_posix()
                if rel in seen_files[grp]:
                    continue
                seen_files[grp].add(rel)
                verify_text_file_sanitized(fp)
                bname = classify_archive_file(grp, rel)
                buckets_by_group[grp][bname].append((fp, f"{prefix}/{rel}"))

    tier_map = {
        "md": ("light", "markdown+profile"),
        "images": ("images", "images"),
        "meta-json": ("raw", "meta-json"),
        "platform-srt": ("raw", "platform-srt"),
        "comments-json": ("raw", "comments-json"),
        "raw": ("raw", "meta-json+platform-srt"),
    }

    assets: list[dict] = []
    for grp in groups:
        for bname in ("md", "images", "meta-json", "platform-srt", "comments-json", "raw"):
            entries = sorted(buckets_by_group[grp].get(bname, []), key=lambda x: x[1])
            if not entries:
                continue
            tier, kind = tier_map[bname]
            parts = split_entries_by_raw_size(entries, max_raw_bytes)
            for idx, part in enumerate(parts, 1):
                suffix = "" if len(parts) == 1 else f"-part{idx:02d}"
                arc_path = out_dir / f"{tag}-{grp}-{bname}{suffix}.tar.gz"
                fcnt, rbytes = build_tar_gz(arc_path, part, compresslevel=compresslevel)
                assets.append(
                    {
                        "name": arc_path.name,
                        "module": "archive",
                        "group": grp,
                        "tier": tier,
                        "kind": kind,
                        "files": fcnt,
                        "raw_bytes": rbytes,
                        "archive_bytes": arc_path.stat().st_size,
                        "sha256": sha256_file(arc_path),
                    }
                )

    video_csv_by_group: dict[str, list[dict]] = defaultdict(list)
    dyn_csv_by_group: dict[str, list[dict]] = defaultdict(list)
    art_csv_by_group: dict[str, list[dict]] = defaultdict(list)
    cmt_stats_by_group_kind: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"comments": 0, "carriers": set(), "pinned": 0, "up_comments": 0, "date_max": ""}
    )
    db_counts = {"videos": 0, "dynamics": 0, "articles": 0, "comments": 0, "search_fts": 0}

    with tempfile.TemporaryDirectory(prefix="wmz_arc_db_") as td:
        db_suffix = tag.removeprefix("archive-delta-").replace(".", "-")
        delta_db = Path(td) / f"biliyt-{db_suffix}.db"
        scon = sqlite3.connect(f"file:{delta['snapshot_db']}?mode=ro", uri=True)
        scon.row_factory = sqlite3.Row
        dcon = sqlite3.connect(str(delta_db))
        try:
            for ddl in BILIYT_DDL:
                dcon.execute(ddl)
            dcon.execute(
                "INSERT INTO schema_migrations(version, applied_at, description) VALUES (?, ?, ?)",
                (
                    1,
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    f"Sanitized biliyt archive delta ({date_start} ~ {date_end})",
                ),
            )

            video_cols = [
                "target_id", "platform", "video_id", "aid", "title", "description",
                "uploader", "upload_date", "duration", "view_count", "like_count",
                "comment_count", "webpage_url", "subtitle_text", "video_relpath",
                "info_json_relpath", "subtitle_relpath", "thumbnail_relpath",
                "raw_json", "archived_at", "updated_at",
            ]
            dyn_cols = [
                "target_id", "dynamic_id", "dynamic_type", "published_at", "pub_ts",
                "author_name", "text_content", "markdown_content", "is_forward",
                "orig_dynamic_id", "orig_text", "like_count", "comment_count",
                "forward_count", "comment_oid", "comment_type", "images_json",
                "folder_relpath", "raw_json", "archived_at",
            ]
            art_cols = [
                "target_id", "cv_id", "opus_id", "title", "category", "published_at",
                "pub_ts", "summary", "text_content", "markdown_content", "words",
                "view_count", "like_count", "reply_count", "images_json",
                "folder_relpath", "raw_json", "archived_at",
            ]
            cmt_cols = [
                "target_id", "carrier_kind", "oid", "rtype", "rpid", "root_rpid",
                "author_mid", "author_name", "is_up", "is_pinned", "has_up_reply",
                "content", "like_count", "ctime", "published_at", "fetched_at",
            ]

            carrier_oids: list[tuple[str, str, str]] = []
            wanted_fts_direct: set[tuple[str, str]] = set()

            for item in delta["videos"]:
                r = item["row"]
                dcon.execute(
                    f"INSERT OR REPLACE INTO videos({','.join(video_cols)}) VALUES ({','.join('?' for _ in video_cols)})",
                    [r.get(c) for c in video_cols],
                )
                db_counts["videos"] += 1
                wanted_fts_direct.add((r["target_id"], str(r["video_id"])))
                if r.get("aid"):
                    carrier_oids.append((r["target_id"], "video", str(r["aid"])))

            for item in delta["dynamics"]:
                r = item["row"]
                dcon.execute(
                    f"INSERT OR REPLACE INTO dynamics({','.join(dyn_cols)}) VALUES ({','.join('?' for _ in dyn_cols)})",
                    [r.get(c) for c in dyn_cols],
                )
                db_counts["dynamics"] += 1
                did = str(r["dynamic_id"])
                wanted_fts_direct.add((r["target_id"], did))
                oid = str(r.get("comment_oid") or did)
                carrier_oids.append((r["target_id"], "dynamic", oid))

            for item in delta["articles"]:
                r = item["row"]
                dcon.execute(
                    f"INSERT OR REPLACE INTO articles({','.join(art_cols)}) VALUES ({','.join('?' for _ in art_cols)})",
                    [r.get(c) for c in art_cols],
                )
                db_counts["articles"] += 1
                cv = str(r["cv_id"])
                wanted_fts_direct.add((r["target_id"], cv))
                carrier_oids.append((r["target_id"], "article", cv))

            cmt_count_by_carrier: dict[tuple[str, str, str], int] = defaultdict(int)
            wanted_cmt_prefixes: set[tuple[str, str]] = set()
            for tid, ckind, oid in carrier_oids:
                wanted_cmt_prefixes.add((tid, f"{ckind}:{oid}:"))
                rows = scon.execute(
                    f"SELECT {','.join(cmt_cols)} FROM comments "
                    "WHERE target_id = ? AND carrier_kind = ? AND oid = ?",
                    (tid, ckind, oid),
                ).fetchall()
                for cr in rows:
                    dcon.execute(
                        f"INSERT OR REPLACE INTO comments({','.join(cmt_cols)}) VALUES ({','.join('?' for _ in cmt_cols)})",
                        [cr[c] for c in cmt_cols],
                    )
                    db_counts["comments"] += 1
                    cmt_count_by_carrier[(tid, ckind, oid)] += 1
                    st = cmt_stats_by_group_kind[(tid, ckind)]
                    st["comments"] += 1
                    st["carriers"].add(oid)
                    st["pinned"] += int(cr["is_pinned"] or 0)
                    st["up_comments"] += int(cr["is_up"] or 0)
                    pd = str(cr["published_at"] or "")[:10]
                    if pd > st["date_max"]:
                        st["date_max"] = pd

            fts_cols = [
                "target_id", "kind", "item_id", "display_title", "title", "body",
                "published_at", "source_url",
            ]
            ph_g = ",".join("?" for _ in groups)
            for fr in scon.execute(
                f"SELECT {','.join(fts_cols)} FROM search_fts WHERE target_id IN ({ph_g})",
                groups,
            ):
                tid = str(fr["target_id"])
                kind = str(fr["kind"])
                iid = str(fr["item_id"])
                keep = False
                if kind in ("video", "subtitle", "dynamic", "article"):
                    keep = (tid, iid) in wanted_fts_direct
                elif kind == "comment":
                    keep = any(
                        tid == ptid and iid.startswith(pfx)
                        for ptid, pfx in wanted_cmt_prefixes
                    )
                if keep:
                    dcon.execute(
                        f"INSERT INTO search_fts({','.join(fts_cols)}) VALUES ({','.join('?' for _ in fts_cols)})",
                        [fr[c] for c in fts_cols],
                    )
                    db_counts["search_fts"] += 1

            dcon.commit()
            dcon.execute("PRAGMA journal_mode = DELETE")
            dcon.execute("PRAGMA optimize")
            dcon.execute("VACUUM")

            for item in delta["videos"]:
                r = item["row"]
                grp = item["group"]
                vid = str(r["video_id"])
                aid = str(r.get("aid") or "")
                has_srt = bool(r.get("subtitle_relpath")) or any(
                    f.suffix.lower() == ".srt" for f in item["files"]
                )
                row_dict = {
                    "video_id": vid,
                    "upload_date": r.get("upload_date") or "",
                    "title": r.get("title") or "",
                    "duration_sec": (
                        round(float(r["duration"]), 1)
                        if r.get("duration") is not None
                        else ""
                    ),
                    "view_count": r["view_count"] if r.get("view_count") is not None else "",
                    "like_count": r["like_count"] if r.get("like_count") is not None else "",
                    "comment_count": r["comment_count"] if r.get("comment_count") is not None else "",
                    "has_platform_srt": 1 if has_srt else 0,
                    "source_url": r.get("webpage_url") or make_source_url(grp, vid),
                }
                if grp == "bili_23191782":
                    row_dict["aid"] = aid
                    row_dict["archived_comments"] = cmt_count_by_carrier.get(
                        (grp, "video", aid), 0
                    )
                video_csv_by_group[grp].append(row_dict)

            for item in delta["dynamics"]:
                r = item["row"]
                grp = item["group"]
                did = str(r["dynamic_id"])
                oid = str(r.get("comment_oid") or did)
                try:
                    imgs = json.loads(r.get("images_json") or "[]")
                except Exception:
                    imgs = []
                dyn_csv_by_group[grp].append(
                    {
                        "dynamic_id": did,
                        "published_at": r.get("published_at") or "",
                        "dynamic_type": r.get("dynamic_type") or "",
                        "is_forward": int(r.get("is_forward") or 0),
                        "like_count": r["like_count"] if r.get("like_count") is not None else "",
                        "comment_count": r["comment_count"] if r.get("comment_count") is not None else "",
                        "forward_count": r["forward_count"] if r.get("forward_count") is not None else "",
                        "archived_comments": cmt_count_by_carrier.get((grp, "dynamic", oid), 0),
                        "image_count": len(imgs),
                        "text_preview": clean_preview(r.get("text_content"), 120),
                        "source_url": f"https://t.bilibili.com/{did}",
                    }
                )

            for item in delta["articles"]:
                r = item["row"]
                grp = item["group"]
                cv = str(r["cv_id"])
                try:
                    imgs = json.loads(r.get("images_json") or "[]")
                except Exception:
                    imgs = []
                art_csv_by_group[grp].append(
                    {
                        "cv_id": cv,
                        "opus_id": r.get("opus_id") or "",
                        "published_at": r.get("published_at") or "",
                        "title": r.get("title") or "",
                        "category": r.get("category") or "",
                        "words": r["words"] if r.get("words") is not None else "",
                        "view_count": r["view_count"] if r.get("view_count") is not None else "",
                        "like_count": r["like_count"] if r.get("like_count") is not None else "",
                        "reply_count": r["reply_count"] if r.get("reply_count") is not None else "",
                        "archived_comments": cmt_count_by_carrier.get((grp, "article", cv), 0),
                        "image_count": len(imgs),
                        "summary_preview": clean_preview(r.get("summary"), 120),
                        "source_url": f"https://www.bilibili.com/read/cv{cv}",
                    }
                )
        finally:
            dcon.close()
            scon.close()

        verify_db_sanitized(
            delta_db,
            ("videos", "dynamics", "articles", "comments", "search_fts", "sync_runs"),
            {
                "videos": "target_id",
                "dynamics": "target_id",
                "articles": "target_id",
                "comments": "target_id",
                "search_fts": "target_id",
            },
        )

        db_arc = out_dir / f"{tag}-biliyt-db.tar.gz"
        fcnt, rbytes = build_tar_gz(
            db_arc, [(delta_db, f"data/db/deltas/{delta_db.name}")], compresslevel=compresslevel
        )
        assets.append(
            {
                "name": db_arc.name,
                "module": "archive",
                "group": "_meta",
                "tier": "light",
                "kind": "sqlite-db",
                "files": fcnt,
                "raw_bytes": rbytes,
                "archive_bytes": db_arc.stat().st_size,
                "sha256": sha256_file(db_arc),
            }
        )

    oversized = [a for a in assets if a["archive_bytes"] >= GITHUB_ASSET_LIMIT_BYTES]
    if oversized:
        raise RuntimeError(f"Archive delta assets exceed 2 GiB cap: {oversized}")

    manifest = {
        "schema_version": 1,
        "release_type": "delta",
        "base_tag": base_tag,
        "date_range": {"start": date_start, "end": date_end},
        "tag": tag,
        "repo": repo,
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "modules": ["archive"],
        "groups": groups,
        "total_archive_bytes": sum(a["archive_bytes"] for a in assets),
        "total_raw_bytes": sum(a["raw_bytes"] for a in assets),
        "assets": assets,
    }
    manifest_path = out_dir / "MANIFEST.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    sums = [f"{a['sha256']}  {a['name']}" for a in assets]
    sums.append(f"{sha256_file(manifest_path)}  {manifest_path.name}")
    (out_dir / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")

    light_b = sum(a["archive_bytes"] for a in assets if a["tier"] == "light")
    full_b = manifest["total_archive_bytes"]
    notes = [
        f"## 多平台内容归档增量包 `{tag}`（{date_start} ~ {date_end}）",
        "",
        f"本 Release 为基线快照 [`{base_tag}`](https://github.com/{repo}/releases/tag/{base_tag}) 之后的**不可变时间段增量包**，覆盖 **{date_start} ~ {date_end}** 期间新增内容：",
        "",
        f"- **新增视频**：{db_counts['videos']} 条",
        f"- **新增动态**：{db_counts['dynamics']} 条",
        f"- **新增专栏**：{db_counts['articles']} 篇",
        f"- **新增精选评论**：{db_counts['comments']} 条",
        f"- **新增 FTS5 索引行**：{db_counts['search_fts']} 行",
        "",
        "### 快速下载与还原",
        "",
        "```bash",
        "# 一键还原最新基线 + 全部增量（自动合并 biliyt.db 与 FTS5）",
        "python scripts/restore.py --modules archive --preset light",
        "",
        "# 仅拉取本期增量并合入已有 data/",
        f"python scripts/restore.py --tag {tag} --preset light  # {format_size(light_b)}",
        f"python scripts/restore.py --tag {tag} --preset full   # {format_size(full_b)}",
        "```",
        "",
        "### 资产清单",
        "",
        "| Asset | Group | Tier | Type | Files | Raw Size | Archive Size |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for a in assets:
        notes.append(
            f"| `{a['name']}` | `{a['group']}` | `{a['tier']}` | {a['kind']} | "
            f"{a['files']:,} | {format_size(a['raw_bytes'])} | {format_size(a['archive_bytes'])} |"
        )
    notes.extend(
        [
            "",
            f"**总计**：{len(assets)} 个归档分包 | 原始体积 {format_size(manifest['total_raw_bytes'])} → 压缩体积 {format_size(full_b)}",
            "",
            "### 完整性校验",
            "",
            "```bash",
            "sha256sum -c SHA256SUMS.txt",
            "```",
        ]
    )
    (out_dir / "RELEASE_NOTES.md").write_text("\n".join(notes) + "\n", encoding="utf-8")

    cmt_summary = {
        k: {
            "comments": v["comments"],
            "carriers": len(v["carriers"]),
            "pinned": v["pinned"],
            "up_comments": v["up_comments"],
            "date_max": v["date_max"],
        }
        for k, v in cmt_stats_by_group_kind.items()
    }
    bucket_file_counts = {
        grp: {b: len(v) for b, v in bmap.items()}
        for grp, bmap in buckets_by_group.items()
    }
    return {
        "module": "archive",
        "tag": tag,
        "title": f"Multi-Platform Content Archive Delta {date_start} ~ {date_end} ({tag})",
        "out_dir": out_dir,
        "manifest": manifest,
        "date_start": date_start,
        "date_end": date_end,
        "db_counts": db_counts,
        "video_csv_by_group": dict(video_csv_by_group),
        "dyn_csv_by_group": dict(dyn_csv_by_group),
        "art_csv_by_group": dict(art_csv_by_group),
        "cmt_summary": cmt_summary,
        "bucket_file_counts": bucket_file_counts,
    }


# ---------------------------------------------------------------------------
# Repo Metadata, Smoke Verification, and Atomic GitHub Publishing
# ---------------------------------------------------------------------------


def smoke_verify_release(out_dir: Path, repo_root: Path) -> None:
    """Run offline scripts/restore.py against staged assets before publishing."""
    restore_script = repo_root / "scripts" / "restore.py"
    with tempfile.TemporaryDirectory(prefix="wmz_smoke_") as td:
        cmd = [
            sys.executable,
            str(restore_script),
            "--src-dir",
            str(out_dir),
            "--preset",
            "full",
            "--dest",
            td,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"Offline restore smoke test FAILED for {out_dir.name}:\n"
                f"{proc.stdout}\n{proc.stderr}"
            )


def merge_csv(
    csv_path: Path,
    new_rows: list[dict],
    key_col: str,
    sort_key,
    reverse: bool = False,
) -> list[dict]:
    if not csv_path.is_file():
        raise FileNotFoundError(f"Expected existing catalog CSV: {csv_path}")
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        existing = list(reader)

    by_key = {r[key_col]: r for r in existing}
    for r in new_rows:
        by_key[r[key_col]] = {k: r.get(k, "") for k in fieldnames}
    merged = sorted(by_key.values(), key=sort_key, reverse=reverse)
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(merged)
    verify_text_file_sanitized(csv_path)
    return merged


def update_repo_metadata(
    repo_root: Path,
    asr_pkg: dict | None,
    arc_pkg: dict | None,
) -> None:
    stats_path = repo_root / "catalog" / "stats.json"
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    delta_tags = dict(stats.get("delta_tags") or {"asr": [], "archive": []})

    if asr_pkg is not None:
        tag = asr_pkg["tag"]
        m_dest = repo_root / "manifests" / tag
        m_dest.mkdir(parents=True, exist_ok=True)
        for fn in ("MANIFEST.json", "SHA256SUMS.txt"):
            shutil.copy2(asr_pkg["out_dir"] / fn, m_dest / fn)
            verify_text_file_sanitized(m_dest / fn)

        asr_mod = stats["modules"]["asr"]
        for grp, new_rows in asr_pkg["csv_rows_by_group"].items():
            csv_path = repo_root / "catalog" / f"asr_{grp}.csv"
            merged = merge_csv(
                csv_path,
                new_rows,
                key_col="video_id",
                sort_key=lambda r: (
                    r.get("upload_date") or "",
                    r.get("txt_rel_path") or "",
                    r.get("video_id") or "",
                ),
            )
            gstat = asr_mod["groups"][grp]
            total_sec = sum(
                float(r["duration_sec"]) for r in merged if r.get("duration_sec")
            )
            dates = [r["upload_date"] for r in merged if r.get("upload_date")]
            gstat["db_records"] = len(merged)
            gstat["asr_done_records"] = sum(
                1 for r in merged if r.get("asr_status") == "done"
            )
            gstat["duration_hours"] = round(total_sec / 3600.0, 2)
            if dates:
                gstat["date_min"] = min(dates)
                gstat["date_max"] = max(dates)
            gstat["files"] = {
                "txt": len({r["txt_rel_path"] for r in merged if r.get("txt_rel_path")}),
                "srt": len({r["srt_rel_path"] for r in merged if r.get("srt_rel_path")}),
                "json": len({r["json_rel_path"] for r in merged if r.get("json_rel_path")}),
            }
            added_b = asr_pkg["added_bytes_by_group"].get(grp, {})
            for ext in ("txt", "srt", "json"):
                gstat["raw_bytes"][ext] = int(gstat["raw_bytes"].get(ext, 0)) + int(
                    added_b.get(ext, 0)
                )

        if tag not in delta_tags.setdefault("asr", []):
            delta_tags["asr"].append(tag)
        asr_mod["delta_tags"] = list(delta_tags["asr"])
        all_g = asr_mod["groups"].values()
        asr_mod["total_db_records"] = sum(g["db_records"] for g in all_g)
        asr_mod["total_txt_files"] = sum(g["files"]["txt"] for g in all_g)
        asr_mod["total_srt_files"] = sum(g["files"]["srt"] for g in all_g)
        asr_mod["total_json_files"] = sum(g["files"]["json"] for g in all_g)
        asr_mod["total_duration_hours"] = round(
            sum(g["duration_hours"] for g in all_g), 2
        )
        max_dates = [g["date_max"] for g in all_g if g.get("date_max")]
        if max_dates:
            asr_mod["content_date_max"] = max(max_dates)

    if arc_pkg is not None:
        tag = arc_pkg["tag"]
        m_dest = repo_root / "manifests" / tag
        m_dest.mkdir(parents=True, exist_ok=True)
        for fn in ("MANIFEST.json", "SHA256SUMS.txt"):
            shutil.copy2(arc_pkg["out_dir"] / fn, m_dest / fn)
            verify_text_file_sanitized(m_dest / fn)

        arc_mod = stats["modules"]["archive"]
        for grp, new_rows in arc_pkg["video_csv_by_group"].items():
            csv_path = repo_root / "catalog" / f"videos_{grp}.csv"
            merged = merge_csv(
                csv_path,
                new_rows,
                key_col="video_id",
                sort_key=lambda r: r.get("video_id") or "",
                reverse=False,
            )
            merged.sort(key=lambda r: r.get("upload_date") or "", reverse=True)
            with csv_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(merged[0].keys()))
                writer.writeheader()
                writer.writerows(merged)
            verify_text_file_sanitized(csv_path)

            gv = arc_mod["groups"][grp]["videos"]
            gv["count"] = len(merged)
            vdates = [r["upload_date"] for r in merged if len(r.get("upload_date") or "") == 10]
            if vdates:
                gv["date_max"] = max(vdates)
            gv["info_json_files"] = int(gv.get("info_json_files", 0)) + len(new_rows)
            gv["platform_srt_files"] = int(gv.get("platform_srt_files", 0)) + sum(
                1 for r in new_rows if str(r.get("has_platform_srt")) == "1"
            )

        for grp, new_rows in arc_pkg["dyn_csv_by_group"].items():
            csv_path = repo_root / "catalog" / f"dynamics_{grp}.csv"
            merged = merge_csv(
                csv_path,
                new_rows,
                key_col="dynamic_id",
                sort_key=lambda r: (r.get("published_at") or "", r.get("dynamic_id") or ""),
                reverse=True,
            )
            gd = arc_mod["groups"][grp]["dynamics"]
            gd["count"] = len(merged)
            ddates = [r["published_at"][:10] for r in merged if len(r.get("published_at") or "") >= 10]
            if ddates:
                gd["date_max"] = max(ddates)

        for grp, new_rows in arc_pkg["art_csv_by_group"].items():
            csv_path = repo_root / "catalog" / f"articles_{grp}.csv"
            merged = merge_csv(
                csv_path,
                new_rows,
                key_col="cv_id",
                sort_key=lambda r: (r.get("published_at") or "", r.get("cv_id") or ""),
                reverse=True,
            )
            ga = arc_mod["groups"][grp]["articles"]
            ga["count"] = len(merged)
            adates = [r["published_at"][:10] for r in merged if len(r.get("published_at") or "") >= 10]
            if adates:
                ga["date_max"] = max(adates)

        for (grp, ckind), csum in arc_pkg["cmt_summary"].items():
            section = {"video": "videos", "dynamic": "dynamics", "article": "articles"}[ckind]
            cobj = arc_mod["groups"][grp][section]["comments"]
            cobj["comments"] = int(cobj.get("comments", 0)) + csum["comments"]
            cobj["carriers"] = int(cobj.get("carriers", 0)) + csum["carriers"]
            cobj["pinned"] = int(cobj.get("pinned", 0)) + csum["pinned"]
            cobj["up_comments"] = int(cobj.get("up_comments", 0)) + csum["up_comments"]
            if csum["date_max"] > str(cobj.get("date_max") or ""):
                cobj["date_max"] = csum["date_max"]

        for grp, bcounts in arc_pkg["bucket_file_counts"].items():
            gobj = arc_mod["groups"][grp]
            gobj["images_files"] = int(gobj.get("images_files", 0)) + int(
                bcounts.get("images", 0)
            )
            gobj["total_files"] = int(gobj.get("total_files", 0)) + sum(bcounts.values())

        if tag not in delta_tags.setdefault("archive", []):
            delta_tags["archive"].append(tag)
        arc_mod["delta_tags"] = list(delta_tags["archive"])
        all_g = arc_mod["groups"].values()
        arc_mod["total_videos"] = sum(g["videos"]["count"] for g in all_g)
        arc_mod["total_dynamics"] = sum(g.get("dynamics", {}).get("count", 0) for g in all_g)
        arc_mod["total_articles"] = sum(g.get("articles", {}).get("count", 0) for g in all_g)
        arc_mod["total_comments"] = sum(
            g["videos"]["comments"].get("comments", 0)
            + g.get("dynamics", {}).get("comments", {}).get("comments", 0)
            + g.get("articles", {}).get("comments", {}).get("comments", 0)
            for g in all_g
        )
        arc_mod["total_images"] = sum(g.get("images_files", 0) for g in all_g)
        arc_mod["total_platform_srt_files"] = sum(
            g["videos"].get("platform_srt_files", 0) for g in all_g
        )
        arc_mod["content_date_max"] = max(
            str(arc_mod.get("content_date_max") or ""), arc_pkg["date_end"]
        )

    stats["delta_tags"] = delta_tags
    stats["updated_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats_path.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    verify_text_file_sanitized(stats_path)
    update_readme_snapshot_section(repo_root, stats)


def update_readme_snapshot_section(repo_root: Path, stats: dict) -> None:
    """Update the bounded snapshot summary block in README.md if markers exist."""
    readme_path = repo_root / "README.md"
    if not readme_path.is_file():
        return
    content = readme_path.read_text(encoding="utf-8")
    if README_TABLE_START not in content or README_TABLE_END not in content:
        return

    repo = DEFAULT_REPO
    asr = stats["modules"]["asr"]
    arc = stats["modules"]["archive"]
    asr_base = asr["latest_tag"]
    arc_base = arc["latest_tag"]
    asr_deltas = asr.get("delta_tags") or []
    arc_deltas = arc.get("delta_tags") or []

    def manifest_range_and_size(tag: str) -> tuple[str, str]:
        mp = repo_root / "manifests" / tag / "MANIFEST.json"
        if not mp.is_file():
            return ("-", "-")
        m = json.loads(mp.read_text(encoding="utf-8"))
        dr = m.get("date_range") or {}
        span = f"{dr.get('start', '?')} ~ {dr.get('end', '?')}" if dr else "-"
        return span, format_size(int(m.get("total_archive_bytes", 0)))

    lines = [
        README_TABLE_START,
        "| 发布流 | 类型与版本 | 时间跨度 | 覆盖内容 | 在线目录与账本 |",
        "| --- | --- | --- | --- | --- |",
        f"| **ASR 转录** (`asr`) | 基线 [`{asr_base}`](https://github.com/{repo}/releases/tag/{asr_base}) | 2017-12-14 ~ 2026-07-17 | B 站 1,980 条 / 1,530.03h + YouTube 105 条 / 154.29h（共 2,084 组 `.txt`/`.srt`、1,979 份词级 `.json`、`asr_pipeline.db`） | [`asr_bili`](catalog/asr_bili_23191782.csv) · [`asr_yt`](catalog/asr_yt_wujixuanshi.csv) · [`MANIFEST`](manifests/{asr_base}/MANIFEST.json) |",
    ]
    for dtag in asr_deltas:
        span, sz = manifest_range_and_size(dtag)
        lines.append(
            f"| **ASR 转录** (`asr`) | 增量 [`{dtag}`](https://github.com/{repo}/releases/tag/{dtag}) | {span} | 时间段新增语音转录产物与脱敏增量库（压缩 {sz}） | [`MANIFEST`](manifests/{dtag}/MANIFEST.json) |"
        )
    lines.append(
        f"| **多平台归档** (`archive`) | 基线 [`{arc_base}`](https://github.com/{repo}/releases/tag/{arc_base}) | 2017-12-14 ~ 2026-11-27 | 视频元数据 1,797（B 站 1,691 + YT 106）、动态 2,773、专栏 71、精选评论 216,882、平台自带 `.srt` 1,905、图片/封面 4,525、`biliyt.db` | [`videos_bili`](catalog/videos_bili_23191782.csv) · [`videos_yt`](catalog/videos_yt_wujixuanshi.csv) · [`dynamics`](catalog/dynamics_bili_23191782.csv) · [`articles`](catalog/articles_bili_23191782.csv) · [`MANIFEST`](manifests/{arc_base}/MANIFEST.json) |"
    )
    for dtag in arc_deltas:
        span, sz = manifest_range_and_size(dtag)
        lines.append(
            f"| **多平台归档** (`archive`) | 增量 [`{dtag}`](https://github.com/{repo}/releases/tag/{dtag}) | {span} | 时间段新增视频/动态/专栏/评论与增量 FTS5 库（压缩 {sz}） | [`MANIFEST`](manifests/{dtag}/MANIFEST.json) |"
        )
    lines.append("")
    lines.append(
        f"> 累计覆盖（基线 + 增量）：ASR 共 **{asr['total_db_records']:,} 条 / {asr['total_duration_hours']:,.2f}h**（`2017-12-14 ~ {asr['content_date_max']}`）；"
        f"多平台归档共 **{arc['total_videos']:,} 条视频、{arc['total_dynamics']:,} 条动态、{arc['total_articles']:,} 篇专栏、{arc['total_comments']:,} 条精选评论**。"
        "完整统计见 [`catalog/stats.json`](catalog/stats.json)。"
    )
    lines.append(README_TABLE_END)

    start_idx = content.index(README_TABLE_START)
    end_idx = content.index(README_TABLE_END) + len(README_TABLE_END)
    updated = content[:start_idx] + "\n".join(lines) + content[end_idx:]
    readme_path.write_text(updated, encoding="utf-8")


def upload_draft_release(pkg: dict, repo: str) -> None:
    out_dir: Path = pkg["out_dir"]
    tag: str = pkg["tag"]
    title: str = pkg["title"]
    notes_path = out_dir / "RELEASE_NOTES.md"
    asset_paths = [
        str(out_dir / "MANIFEST.json"),
        str(out_dir / "SHA256SUMS.txt"),
    ] + [str(out_dir / a["name"]) for a in pkg["manifest"]["assets"]]

    log(f"Creating DRAFT GitHub Release {tag} ({len(asset_paths)} files)...")
    run_cmd_with_retry(
        [
            "gh",
            "release",
            "create",
            tag,
            "-R",
            repo,
            "--draft",
            "--title",
            title,
            "--notes-file",
            str(notes_path),
            *asset_paths,
        ],
        label=f"gh release create --draft {tag}",
    )


def undraft_release(pkg: dict, repo: str) -> None:
    tag: str = pkg["tag"]
    log(f"Publishing Release {tag} (--draft=false)...")
    run_cmd_with_retry(
        ["gh", "release", "edit", tag, "-R", repo, "--draft=false"],
        label=f"gh release edit --draft=false {tag}",
    )


def git_sync_main(repo_root: Path) -> None:
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if status:
        raise RuntimeError(
            f"Refusing to publish with dirty git working tree in {repo_root}:\n{status}"
        )
    run_cmd_with_retry(
        ["git", "pull", "--ff-only", "origin", "main"],
        cwd=repo_root,
        label="git pull --ff-only origin main",
    )


def git_commit_and_push(repo_root: Path, tags: list[str]) -> None:
    run_cmd_with_retry(
        ["git", "add", "catalog/", "manifests/", "README.md"],
        cwd=repo_root,
        label="git add metadata",
    )
    msg = f"chore(release): publish incremental delta ({', '.join(tags)})"
    subprocess.run(["git", "commit", "-m", msg], cwd=str(repo_root), check=True)
    run_cmd_with_retry(
        ["git", "push", "origin", "main"],
        cwd=repo_root,
        label="git push origin main",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Automatically detect, package, and publish immutable time-range delta releases."
    )
    parser.add_argument(
        "--module",
        "--modules",
        dest="modules",
        nargs="+",
        choices=("all", "asr", "archive"),
        default=["all"],
        help="Streams to check/package (default: all).",
    )
    parser.add_argument(
        "--asr-root",
        type=Path,
        default=DEFAULT_ASR_ROOT,
        help=f"Upstream qwen3-asr-pipeline root (default: {DEFAULT_ASR_ROOT}).",
    )
    parser.add_argument(
        "--biliyt-root",
        type=Path,
        default=DEFAULT_BILIYT_ROOT,
        help=f"Upstream biliyt-archiver root (default: {DEFAULT_BILIYT_ROOT}).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help=f"Local weimingzi_archive clone root (default: {REPO_ROOT}).",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"GitHub repository slug (default: {DEFAULT_REPO}).",
    )
    parser.add_argument(
        "--dist-root",
        type=Path,
        default=None,
        help="Staging directory for .tar.gz assets (default: <repo-root>/release_dist).",
    )
    parser.add_argument(
        "--max-part-raw-mb",
        type=int,
        default=DEFAULT_MAX_PART_RAW_MB,
        help=f"Split archive parts above this raw MB (default: {DEFAULT_MAX_PART_RAW_MB}).",
    )
    parser.add_argument("--level", type=int, default=6, choices=range(1, 10))
    parser.add_argument(
        "--wait-lock-sec",
        type=int,
        default=600,
        help="Max seconds to wait for upstream biliyt/asr locks (default: 600).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect deltas and report what would be packaged/published without writing anything.",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Update repo metadata, upload draft GitHub Releases, push main, undraft Releases, and clean dist.",
    )
    parser.add_argument(
        "--skip-git-pull",
        action="store_true",
        help="Skip initial `git pull --ff-only origin main` (useful for local tests).",
    )
    parser.add_argument(
        "--keep-dist",
        action="store_true",
        help="Keep release_dist/ staging artifacts after publishing.",
    )

    args = parser.parse_args()
    repo_root: Path = args.repo_root.resolve()
    dist_root: Path = (args.dist_root or (repo_root / "release_dist")).resolve()
    wanted = {"asr", "archive"} if "all" in args.modules else set(args.modules)

    pub_lock_fh = None
    if fcntl is not None and not args.dry_run:
        lock_file = Path(tempfile.gettempdir()) / "weimingzi_archive_publish.lock"
        pub_lock_fh = lock_file.open("w")
        try:
            fcntl.flock(pub_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Another package_delta.py publish process is already running; exiting.")
            return 0

    try:
        if args.publish and not args.skip_git_pull:
            git_sync_main(repo_root)

        if not args.dry_run:
            wait_for_upstream_locks(
                [
                    args.biliyt_root / "state" / ".sync.lock",
                    Path("/opt/biliyt-asr-job/runtime/.job.lock"),
                ],
                timeout_sec=args.wait_lock_sec,
            )

        stats = json.loads(
            (repo_root / "catalog" / "stats.json").read_text(encoding="utf-8")
        )
        remote_tags = list_remote_release_tags(args.repo) if args.publish else set()

        with tempfile.TemporaryDirectory(prefix="wmz_delta_snap_") as snap_td:
            snap_dir = Path(snap_td)
            asr_delta = (
                detect_asr_delta(args.asr_root, repo_root, snap_dir)
                if "asr" in wanted
                else None
            )
            arc_delta = (
                detect_archive_delta(args.biliyt_root, repo_root, snap_dir)
                if "archive" in wanted
                else None
            )

            if asr_delta is None and arc_delta is None:
                log("No new ASR or archive items detected beyond catalog/*.csv. Nothing to publish.")
                return 0

            asr_tag = (
                pick_unique_delta_tag(
                    "asr",
                    asr_delta["date_start"],
                    asr_delta["date_end"],
                    repo_root,
                    remote_tags,
                )
                if asr_delta
                else None
            )
            arc_tag = (
                pick_unique_delta_tag(
                    "archive",
                    arc_delta["date_start"],
                    arc_delta["date_end"],
                    repo_root,
                    remote_tags,
                )
                if arc_delta
                else None
            )

            if asr_delta:
                log(
                    f"[asr] Detected {len(asr_delta['items'])} new transcript(s) "
                    f"({asr_delta['date_start']} ~ {asr_delta['date_end']}) -> tag {asr_tag}"
                )
            if arc_delta:
                log(
                    f"[archive] Detected {len(arc_delta['videos'])} video(s), "
                    f"{len(arc_delta['dynamics'])} dynamic(s), "
                    f"{len(arc_delta['articles'])} article(s) "
                    f"({arc_delta['date_start']} ~ {arc_delta['date_end']}) -> tag {arc_tag}"
                )

            if args.dry_run:
                log("Dry-run complete; no files written or published.")
                return 0

            asr_pkg = (
                package_asr_delta(
                    asr_delta,
                    tag=asr_tag,
                    base_tag=stats["latest_tags"]["asr"],
                    repo=args.repo,
                    out_dir=dist_root / asr_tag,
                    max_part_raw_mb=args.max_part_raw_mb,
                    compresslevel=args.level,
                )
                if asr_delta and asr_tag
                else None
            )
            arc_pkg = (
                package_archive_delta(
                    arc_delta,
                    tag=arc_tag,
                    base_tag=stats["latest_tags"]["archive"],
                    repo=args.repo,
                    out_dir=dist_root / arc_tag,
                    max_part_raw_mb=args.max_part_raw_mb,
                    compresslevel=args.level,
                )
                if arc_delta and arc_tag
                else None
            )

        packages = [p for p in (asr_pkg, arc_pkg) if p is not None]
        for pkg in packages:
            log(
                f"Staged {pkg['tag']}: {len(pkg['manifest']['assets'])} asset(s), "
                f"{format_size(pkg['manifest']['total_raw_bytes'])} -> "
                f"{format_size(pkg['manifest']['total_archive_bytes'])}"
            )
            smoke_verify_release(pkg["out_dir"], repo_root)
            log(f"[OK] Offline restore smoke verification passed for {pkg['tag']}")

        if not args.publish:
            log("Package-only mode complete (pass --publish to commit, push, and publish Releases).")
            return 0

        if not shutil.which("gh"):
            raise RuntimeError("GitHub CLI `gh` not found in PATH.")

        for pkg in packages:
            upload_draft_release(pkg, args.repo)

        update_repo_metadata(repo_root, asr_pkg, arc_pkg)
        git_commit_and_push(repo_root, [p["tag"] for p in packages])

        for pkg in packages:
            undraft_release(pkg, args.repo)
            log(f"[PUBLISHED] https://github.com/{args.repo}/releases/tag/{pkg['tag']}")

        if not args.keep_dist:
            for pkg in packages:
                shutil.rmtree(pkg["out_dir"], ignore_errors=True)
        return 0
    finally:
        if pub_lock_fh is not None:
            try:
                fcntl.flock(pub_lock_fh.fileno(), fcntl.LOCK_UN)
                pub_lock_fh.close()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
