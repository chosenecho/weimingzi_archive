#!/usr/bin/env python3
"""Package and publish multi-platform biliyt archive snapshots for weimingzi_archive.

Pure Python standard library implementation.
Enforces strict creator whitelist (bili_23191782, yt_wujixuanshi), excludes raw
video/audio binaries (.mp4 etc.), merges & sanitizes biliyt.db (filtering out
non-whitelisted creators and local run history), splits large tiers (< 2 GiB
each) into UTF-8 PAX .tar.gz release bundles, generates online catalog CSVs and
stats, and publishes independent `archive-vYYYY.MM.DD` GitHub Releases via `gh`.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "chosenecho/weimingzi_archive"
DEFAULT_BILIYT_ROOT = Path(r"C:\WorkSpace\bli-youtube")
DEFAULT_VIDEOS_ROOT = Path(r"D:\bili_yt_dl")
DEFAULT_DIST_ROOT = REPO_ROOT / "release_dist"
DEFAULT_MAX_PART_RAW_MB = 450
GITHUB_ASSET_LIMIT_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB hard GitHub limit

ALLOWED_GROUPS: tuple[str, ...] = ("bili_23191782", "yt_wujixuanshi")
GROUP_PLATFORMS: dict[str, str] = {
    "bili_23191782": "bilibili",
    "yt_wujixuanshi": "youtube",
}

TAG_RE = re.compile(
    r"^archive-(?:base-\d{4}\.\d{2}\.\d{2}-\d{4}\.\d{2}\.\d{2}|v\d{4}\.\d{2}\.\d{2})(-r\d+)?$"
)
DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*-\s*")
BV_RE = re.compile(r"\[(BV[0-9A-Za-z]+)(?:_p(\d+))?\]")

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
FORBIDDEN_PATH_TOKENS: tuple[str, ...] = (
    "C:\\",
    "D:\\",
    "c:\\",
    "d:\\",
    "ChosenEcho",
    "SESSDATA",
    "bili_jct",
    "modal.run",
)

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
    CREATE INDEX IF NOT EXISTS idx_profile_snapshots_target
    ON profile_snapshots(target_id, fetched_at)
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
    CREATE INDEX IF NOT EXISTS idx_videos_target_date
    ON videos(target_id, upload_date DESC)
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
    CREATE INDEX IF NOT EXISTS idx_dynamics_target_ts
    ON dynamics(target_id, pub_ts DESC)
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
    CREATE INDEX IF NOT EXISTS idx_articles_target_ts
    ON articles(target_id, pub_ts DESC)
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
    CREATE INDEX IF NOT EXISTS idx_comments_carrier
    ON comments(target_id, carrier_kind, oid)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_comments_up
    ON comments(target_id, is_up, is_pinned)
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


def validate_groups(requested: list[str] | None) -> list[str]:
    if not requested:
        return list(ALLOWED_GROUPS)
    invalid = [g for g in requested if g not in ALLOWED_GROUPS]
    if invalid:
        raise ValueError(
            f"Disallowed groups: {invalid}. Allowed whitelist: {list(ALLOWED_GROUPS)}"
        )
    return [g for g in ALLOWED_GROUPS if g in requested]


def validate_tag(tag: str, allow_custom: bool = False) -> str:
    if not allow_custom and not TAG_RE.match(tag):
        raise ValueError(
            f"Tag '{tag}' must follow archive-base-YYYY.MM.DD-YYYY.MM.DD "
            "(or legacy archive-vYYYY.MM.DD). Pass --allow-custom-tag to override."
        )
    return tag


def detect_latest_dynamic_tag(biliyt_root: Path) -> str:
    """Detect archive-base-YYYY.MM.DD-YYYY.MM.DD from bili_23191782 content date span."""
    earliest = ""
    latest = ""
    dyn_dir = biliyt_root / "downloads" / "bilibili" / "bili_23191782" / "dynamics"
    if dyn_dir.is_dir():
        for entry in dyn_dir.iterdir():
            if entry.is_dir():
                m = DATE_PREFIX_RE.match(entry.name)
                if m:
                    dt = m.group(1)
                    if not earliest or dt < earliest:
                        earliest = dt
                    if dt > latest:
                        latest = dt
    biliyt_db = biliyt_root / "state" / "biliyt.db"
    if biliyt_db.is_file():
        con = sqlite3.connect(f"file:{biliyt_db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT min(substr(published_at, 1, 10)), max(substr(published_at, 1, 10)) FROM dynamics "
                "WHERE target_id = 'bili_23191782' AND published_at IS NOT NULL"
            ).fetchone()
            if row:
                if row[0] and len(row[0]) == 10 and (not earliest or row[0] < earliest):
                    earliest = row[0]
                if row[1] and len(row[1]) == 10 and row[1] > latest:
                    latest = row[1]
        finally:
            con.close()
    if not earliest or not latest:
        raise RuntimeError(
            f"Could not detect dynamic date range under {biliyt_root}"
        )
    return f"archive-base-{earliest.replace('-', '.')}-{latest.replace('-', '.')}"


def clean_preview(text: str | None, max_len: int = 120) -> str:
    if not text:
        return ""
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) > max_len:
        return collapsed[: max_len - 1] + "…"
    return collapsed


def collect_group_files(
    group: str, biliyt_root: Path, videos_root: Path
) -> dict[str, Path]:
    """Collect deduplicated rel_posix -> local Path for an allowed group.

    Excludes video/audio binaries and non-whitelisted directories.
    If the same relative path exists in both videos_root and biliyt_root,
    biliyt_root takes precedence (newer incremental sync).
    """
    platform = GROUP_PLATFORMS[group]
    roots = [
        videos_root / platform / group,
        biliyt_root / "downloads" / platform / group,
    ]
    merged: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, _, filenames in os.walk(root):
            dp = Path(dirpath)
            for fn in filenames:
                fp = dp / fn
                ext = fp.suffix.lower()
                if ext in EXCLUDED_EXTENSIONS:
                    continue
                if fn.startswith(".") or fn.lower() in {"cookies.txt", "config.yaml"}:
                    continue
                rel = fp.relative_to(root).as_posix()
                merged[rel] = fp
    return merged


def classify_group_files(
    group: str, files_map: dict[str, Path]
) -> dict[str, list[tuple[Path, str]]]:
    """Classify collected files into package buckets returning (src_path, arcname)."""
    platform = GROUP_PLATFORMS[group]
    prefix = f"data/archive/{platform}/{group}"
    buckets: dict[str, list[tuple[Path, str]]] = defaultdict(list)

    for rel in sorted(files_map.keys()):
        src = files_map[rel]
        arcname = f"{prefix}/{rel}"
        parts = rel.split("/")
        name_lower = parts[-1].lower()
        ext = Path(name_lower).suffix

        if group == "bili_23191782":
            if parts[0] == "profile":
                buckets["md"].append((src, arcname))
            elif parts[0] in ("dynamics", "articles"):
                if "images" in parts[1:-1] or ext in IMAGE_EXTENSIONS:
                    buckets["images"].append((src, arcname))
                elif name_lower == "comments.json":
                    buckets["comments-json"].append((src, arcname))
                elif name_lower in ("dynamic.json", "article.json"):
                    buckets["meta-json"].append((src, arcname))
                elif name_lower in ("dynamic.md", "article.md", "comments.md"):
                    buckets["md"].append((src, arcname))
                else:
                    raise RuntimeError(
                        f"Unclassified file in {group}/{parts[0]}: {rel}"
                    )
            elif len(parts) == 1:
                if name_lower.endswith(".comments.md"):
                    buckets["md"].append((src, arcname))
                elif name_lower.endswith(".comments.json"):
                    buckets["comments-json"].append((src, arcname))
                elif name_lower.endswith(".info.json"):
                    buckets["meta-json"].append((src, arcname))
                elif ext == ".srt":
                    buckets["platform-srt"].append((src, arcname))
                elif ext in IMAGE_EXTENSIONS:
                    buckets["images"].append((src, arcname))
                else:
                    raise RuntimeError(f"Unclassified root file in {group}: {rel}")
            else:
                raise RuntimeError(f"Unexpected subdirectory in {group}: {rel}")
        elif group == "yt_wujixuanshi":
            if name_lower.endswith(".md"):
                buckets["md"].append((src, arcname))
            elif ext in IMAGE_EXTENSIONS:
                buckets["images"].append((src, arcname))
            elif name_lower.endswith(".info.json") or ext == ".srt" or name_lower.endswith(".comments.json"):
                buckets["raw"].append((src, arcname))
            else:
                raise RuntimeError(f"Unclassified file in {group}: {rel}")
        else:
            raise ValueError(f"Unsupported group: {group}")

    return buckets


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
    """Create a deterministic UTF-8 PAX .tar.gz archive."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
    file_count = 0
    raw_bytes = 0
    try:
        with tarfile.open(
            tmp_path,
            mode="w:gz",
            format=tarfile.PAX_FORMAT,
            encoding="utf-8",
            compresslevel=compresslevel,
        ) as tf:
            for src_path, arcname in entries:
                arcname = arcname.replace("\\", "/").lstrip("/")
                tf.add(src_path, arcname=arcname, recursive=False)
                file_count += 1
                raw_bytes += src_path.stat().st_size
        tmp_path.replace(archive_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return file_count, raw_bytes


def build_bv_date_lookup(files_map: dict[str, Path]) -> dict[str, str]:
    """Map BV id -> earliest YYYY-MM-DD from disk filenames (fixes multi-P 'unknown' dates)."""
    bv_dates: dict[str, str] = {}
    for rel in files_map:
        if "/" in rel:
            continue
        m_date = DATE_PREFIX_RE.match(rel)
        m_bv = BV_RE.search(rel)
        if m_date and m_bv:
            bv = m_bv.group(1)
            dt = m_date.group(1)
            if bv not in bv_dates or dt < bv_dates[bv]:
                bv_dates[bv] = dt
    return bv_dates


def build_merged_sanitized_db(
    biliyt_db: Path,
    videos_db: Path,
    dest_db: Path,
    groups: list[str],
    collected_files: dict[str, dict[str, Path]],
) -> dict:
    """Merge biliyt_db + videos_db strictly for whitelisted groups and sanitize."""
    if dest_db.exists():
        dest_db.unlink()
    dest_db.parent.mkdir(parents=True, exist_ok=True)

    allowed_set = set(groups)
    placeholders = ",".join("?" for _ in groups)
    group_params = tuple(groups)

    bv_date_lookup = (
        build_bv_date_lookup(collected_files["bili_23191782"])
        if "bili_23191782" in collected_files
        else {}
    )

    dst = sqlite3.connect(str(dest_db))
    dst.execute("PRAGMA journal_mode = MEMORY")
    dst.execute("PRAGMA synchronous = OFF")
    for ddl in BILIYT_DDL:
        dst.execute(ddl)
    dst.execute(
        "INSERT INTO schema_migrations(version, applied_at, description) VALUES (?, ?, ?)",
        (
            1,
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Merged & sanitized biliyt archive snapshot (whitelist-filtered)",
        ),
    )

    def copy_table(
        src_con: sqlite3.Connection,
        table: str,
        columns: list[str],
        mode: str = "INSERT OR REPLACE",
        row_filter=None,
    ) -> int:
        col_sql = ", ".join(f'"{c}"' for c in columns)
        qmarks = ", ".join("?" for _ in columns)
        rows = src_con.execute(
            f'SELECT {col_sql} FROM "{table}" WHERE target_id IN ({placeholders})',
            group_params,
        ).fetchall()
        inserted = 0
        batch = []
        for r in rows:
            if row_filter is not None:
                r = row_filter(r)
                if r is None:
                    continue
            batch.append(r)
            inserted += 1
        if batch:
            dst.executemany(
                f'{mode} INTO "{table}" ({col_sql}) VALUES ({qmarks})', batch
            )
        return inserted

    seen_videos: set[tuple[str, str]] = set()
    seen_fts: set[tuple[str, str, str]] = set()

    video_cols = [
        "target_id",
        "platform",
        "video_id",
        "aid",
        "title",
        "description",
        "uploader",
        "upload_date",
        "duration",
        "view_count",
        "like_count",
        "comment_count",
        "webpage_url",
        "subtitle_text",
        "video_relpath",
        "info_json_relpath",
        "subtitle_relpath",
        "thumbnail_relpath",
        "raw_json",
        "archived_at",
        "updated_at",
    ]
    comment_cols = [
        "target_id",
        "carrier_kind",
        "oid",
        "rtype",
        "rpid",
        "root_rpid",
        "author_mid",
        "author_name",
        "is_up",
        "is_pinned",
        "has_up_reply",
        "content",
        "like_count",
        "ctime",
        "published_at",
        "fetched_at",
    ]
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

    # 1) Load historical full videos + video comments from videos_db first
    if videos_db.is_file():
        vcon = sqlite3.connect(f"file:{videos_db}?mode=ro", uri=True)
        try:
            def fix_video_row(r: tuple) -> tuple:
                row = list(r)
                tid, vid, udate = row[0], row[2], row[7]
                if tid == "bili_23191782" and (not udate or udate == "unknown"):
                    if vid in bv_date_lookup:
                        row[7] = bv_date_lookup[vid]
                seen_videos.add((tid, vid))
                return tuple(row)

            copy_table(vcon, "videos", video_cols, "INSERT OR REPLACE", fix_video_row)
            copy_table(vcon, "comments", comment_cols, "INSERT OR REPLACE")

            def dedup_fts_v(r: tuple) -> tuple | None:
                key = (r[0], r[1], r[2])
                if key in seen_fts:
                    return None
                seen_fts.add(key)
                return r

            copy_table(vcon, "search_fts", fts_cols, "INSERT", dedup_fts_v)
        finally:
            vcon.close()

    # 2) Merge profiles, dynamics, articles, dynamic/article comments, and incremental new videos from biliyt_db
    if biliyt_db.is_file():
        bcon = sqlite3.connect(f"file:{biliyt_db}?mode=ro", uri=True)
        try:
            profile_cols = [
                "target_id",
                "platform",
                "uid",
                "name",
                "sign",
                "level",
                "official_title",
                "follower",
                "following",
                "archive_views",
                "likes",
                "avatar_relpath",
                "banner_relpath",
                "collections_json",
                "raw_json",
                "updated_at",
            ]
            snap_cols = [
                "target_id",
                "fetched_at",
                "follower",
                "following",
                "archive_views",
                "likes",
            ]
            dyn_cols = [
                "target_id",
                "dynamic_id",
                "dynamic_type",
                "published_at",
                "pub_ts",
                "author_name",
                "text_content",
                "markdown_content",
                "is_forward",
                "orig_dynamic_id",
                "orig_text",
                "like_count",
                "comment_count",
                "forward_count",
                "comment_oid",
                "comment_type",
                "images_json",
                "folder_relpath",
                "raw_json",
                "archived_at",
            ]
            art_cols = [
                "target_id",
                "cv_id",
                "opus_id",
                "title",
                "category",
                "published_at",
                "pub_ts",
                "summary",
                "text_content",
                "markdown_content",
                "words",
                "view_count",
                "like_count",
                "reply_count",
                "images_json",
                "folder_relpath",
                "raw_json",
                "archived_at",
            ]
            fav_cols = ["target_id", "media_id", "title", "media_count", "updated_at"]
            favi_cols = [
                "target_id",
                "media_id",
                "bvid",
                "title",
                "upper_name",
                "duration",
                "fav_time",
                "url",
            ]

            copy_table(bcon, "profiles", profile_cols, "INSERT OR REPLACE")
            copy_table(bcon, "profile_snapshots", snap_cols, "INSERT")
            copy_table(bcon, "dynamics", dyn_cols, "INSERT OR REPLACE")
            copy_table(bcon, "articles", art_cols, "INSERT OR REPLACE")
            copy_table(bcon, "favorites", fav_cols, "INSERT OR REPLACE")
            copy_table(bcon, "favorite_items", favi_cols, "INSERT OR REPLACE")
            copy_table(bcon, "comments", comment_cols, "INSERT OR REPLACE")

            def merge_incremental_video(r: tuple) -> tuple | None:
                tid, vid, info_rel = r[0], r[2], r[15]
                if (tid, vid) in seen_videos:
                    return None
                # Only import videos that actually have archived files on disk
                gfiles = collected_files.get(tid, {})
                if info_rel and info_rel not in gfiles:
                    return None
                seen_videos.add((tid, vid))
                return r

            copy_table(
                bcon, "videos", video_cols, "INSERT OR IGNORE", merge_incremental_video
            )

            def dedup_fts_b(r: tuple) -> tuple | None:
                tid, kind, item_id = r[0], r[1], r[2]
                key = (tid, kind, item_id)
                if key in seen_fts:
                    return None
                if kind in ("video", "subtitle") and (tid, item_id) not in seen_videos:
                    return None
                seen_fts.add(key)
                return r

            copy_table(bcon, "search_fts", fts_cols, "INSERT", dedup_fts_b)
        finally:
            bcon.close()

    dst.commit()

    # Security & whitelist verification on merged DB
    for tbl in (
        "profiles",
        "profile_snapshots",
        "videos",
        "dynamics",
        "articles",
        "comments",
        "favorites",
        "favorite_items",
        "search_fts",
    ):
        bad = dst.execute(
            f'SELECT count(*) FROM "{tbl}" WHERE target_id NOT IN ({placeholders})',
            group_params,
        ).fetchone()[0]
        if bad > 0:
            raise RuntimeError(
                f"Sanitization check failed: {bad} non-whitelisted rows in {tbl}"
            )

    path_checks = [
        ("profiles", "avatar_relpath"),
        ("profiles", "banner_relpath"),
        ("videos", "video_relpath"),
        ("videos", "info_json_relpath"),
        ("videos", "subtitle_relpath"),
        ("videos", "thumbnail_relpath"),
        ("dynamics", "folder_relpath"),
        ("articles", "folder_relpath"),
    ]
    for tbl, col in path_checks:
        for (val,) in dst.execute(
            f'SELECT "{col}" FROM "{tbl}" WHERE "{col}" IS NOT NULL'
        ):
            for tok in FORBIDDEN_PATH_TOKENS:
                if tok in val:
                    raise RuntimeError(
                        f"Sanitization check failed: forbidden token '{tok}' in {tbl}.{col}: {val}"
                    )

    sync_cnt = dst.execute("SELECT count(*) FROM sync_runs").fetchone()[0]
    if sync_cnt != 0:
        raise RuntimeError(f"Expected empty sync_runs, got {sync_cnt}")

    # Collect summary counts before compacting
    summary = {
        "profiles": dst.execute("SELECT count(*) FROM profiles").fetchone()[0],
        "profile_snapshots": dst.execute(
            "SELECT count(*) FROM profile_snapshots"
        ).fetchone()[0],
        "videos": dst.execute("SELECT count(*) FROM videos").fetchone()[0],
        "dynamics": dst.execute("SELECT count(*) FROM dynamics").fetchone()[0],
        "articles": dst.execute("SELECT count(*) FROM articles").fetchone()[0],
        "comments": dst.execute("SELECT count(*) FROM comments").fetchone()[0],
        "search_fts": dst.execute("SELECT count(*) FROM search_fts").fetchone()[0],
    }

    dst.execute("PRAGMA journal_mode = DELETE")
    dst.execute("PRAGMA optimize")
    dst.execute("VACUUM")
    dst.close()
    return summary


def build_catalogs_and_stats(
    merged_db: Path,
    groups: list[str],
    collected_files: dict[str, dict[str, Path]],
    buckets_by_group: dict[str, dict[str, list[tuple[Path, str]]]],
    tag: str,
) -> tuple[dict[str, tuple[list[str], list[dict]]], dict]:
    """Generate online CSV catalogs and merged stats.json dict from sanitized DB."""
    con = sqlite3.connect(f"file:{merged_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    catalogs: dict[str, tuple[list[str], list[dict]]] = {}
    group_stats: dict[str, dict] = {}

    try:
        # Precompute archived comment counts by (target_id, carrier_kind, oid)
        cmt_counts: dict[tuple[str, str, str], int] = {}
        cmt_totals: dict[tuple[str, str], dict] = {}
        for row in con.execute(
            "SELECT target_id, carrier_kind, oid, count(*) AS cnt "
            "FROM comments GROUP BY target_id, carrier_kind, oid"
        ):
            cmt_counts[(row["target_id"], row["carrier_kind"], str(row["oid"]))] = row[
                "cnt"
            ]
        for row in con.execute(
            "SELECT target_id, carrier_kind, count(*) AS cnt, "
            "count(DISTINCT oid) AS carriers, sum(is_pinned) AS pinned, "
            "sum(is_up) AS up_cnt, min(substr(published_at, 1, 10)) AS dmin, "
            "max(substr(published_at, 1, 10)) AS dmax "
            "FROM comments GROUP BY target_id, carrier_kind"
        ):
            cmt_totals[(row["target_id"], row["carrier_kind"])] = {
                "comments": row["cnt"],
                "carriers": row["carriers"],
                "pinned": row["pinned"] or 0,
                "up_comments": row["up_cnt"] or 0,
                "date_min": row["dmin"],
                "date_max": row["dmax"],
            }

        # Map which video_ids have platform .srt on disk
        srt_video_ids: dict[str, set[str]] = defaultdict(set)
        for grp, files_map in collected_files.items():
            for rel in files_map:
                if "/" not in rel and rel.lower().endswith(".srt"):
                    m_bv = BV_RE.search(rel)
                    if m_bv:
                        srt_video_ids[grp].add(m_bv.group(1))
                    else:
                        m_yt = re.search(r"\[([A-Za-z0-9_-]{11})\]", rel)
                        if m_yt:
                            srt_video_ids[grp].add(m_yt.group(1))

        for grp in groups:
            gstats: dict = {}
            gbuckets = buckets_by_group.get(grp, {})

            # Videos CSV
            v_rows = con.execute(
                "SELECT video_id, aid, upload_date, title, duration, view_count, "
                "like_count, comment_count, webpage_url, subtitle_relpath "
                "FROM videos WHERE target_id = ? "
                "ORDER BY upload_date DESC, video_id ASC",
                (grp,),
            ).fetchall()

            v_csv_rows: list[dict] = []
            v_dates: list[str] = []
            for r in v_rows:
                vid = r["video_id"]
                aid = str(r["aid"] or "")
                ud = r["upload_date"] or ""
                if len(ud) == 10 and ud != "unknown":
                    v_dates.append(ud)
                has_srt = bool(r["subtitle_relpath"]) or (vid in srt_video_ids[grp])
                if grp == "bili_23191782":
                    url = r["webpage_url"] or f"https://www.bilibili.com/video/{vid}"
                    arch_cmts = cmt_counts.get((grp, "video", aid), 0)
                    v_csv_rows.append(
                        {
                            "video_id": vid,
                            "aid": aid,
                            "upload_date": ud,
                            "title": r["title"] or "",
                            "duration_sec": (
                                round(float(r["duration"]), 1)
                                if r["duration"] is not None
                                else ""
                            ),
                            "view_count": r["view_count"] if r["view_count"] is not None else "",
                            "like_count": r["like_count"] if r["like_count"] is not None else "",
                            "comment_count": r["comment_count"] if r["comment_count"] is not None else "",
                            "archived_comments": arch_cmts,
                            "has_platform_srt": 1 if has_srt else 0,
                            "source_url": url,
                        }
                    )
                else:
                    url = r["webpage_url"] or f"https://www.youtube.com/watch?v={vid}"
                    v_csv_rows.append(
                        {
                            "video_id": vid,
                            "upload_date": ud,
                            "title": r["title"] or "",
                            "duration_sec": (
                                round(float(r["duration"]), 1)
                                if r["duration"] is not None
                                else ""
                            ),
                            "view_count": r["view_count"] if r["view_count"] is not None else "",
                            "like_count": r["like_count"] if r["like_count"] is not None else "",
                            "comment_count": r["comment_count"] if r["comment_count"] is not None else "",
                            "has_platform_srt": 1 if has_srt else 0,
                            "source_url": url,
                        }
                    )

            if grp == "bili_23191782":
                v_fields = [
                    "video_id",
                    "aid",
                    "upload_date",
                    "title",
                    "duration_sec",
                    "view_count",
                    "like_count",
                    "comment_count",
                    "archived_comments",
                    "has_platform_srt",
                    "source_url",
                ]
            else:
                v_fields = [
                    "video_id",
                    "upload_date",
                    "title",
                    "duration_sec",
                    "view_count",
                    "like_count",
                    "comment_count",
                    "has_platform_srt",
                    "source_url",
                ]
            catalogs[f"videos_{grp}.csv"] = (v_fields, v_csv_rows)

            gstats["videos"] = {
                "count": len(v_rows),
                "date_min": min(v_dates) if v_dates else None,
                "date_max": max(v_dates) if v_dates else None,
                "info_json_files": sum(
                    1
                    for rel in collected_files.get(grp, {})
                    if "/" not in rel and rel.lower().endswith(".info.json")
                ),
                "platform_srt_files": sum(
                    1
                    for rel in collected_files.get(grp, {})
                    if "/" not in rel and rel.lower().endswith(".srt")
                ),
                "comments": cmt_totals.get((grp, "video"), {"comments": 0, "carriers": 0}),
            }

            # Dynamics CSV (if present)
            d_rows = con.execute(
                "SELECT dynamic_id, published_at, dynamic_type, is_forward, "
                "like_count, comment_count, forward_count, comment_oid, "
                "images_json, text_content "
                "FROM dynamics WHERE target_id = ? "
                "ORDER BY published_at DESC, dynamic_id DESC",
                (grp,),
            ).fetchall()
            if d_rows:
                d_fields = [
                    "dynamic_id",
                    "published_at",
                    "dynamic_type",
                    "is_forward",
                    "like_count",
                    "comment_count",
                    "forward_count",
                    "archived_comments",
                    "image_count",
                    "text_preview",
                    "source_url",
                ]
                d_csv_rows: list[dict] = []
                d_dates: list[str] = []
                for r in d_rows:
                    did = str(r["dynamic_id"])
                    pub = r["published_at"] or ""
                    if len(pub) >= 10:
                        d_dates.append(pub[:10])
                    oid = str(r["comment_oid"] or did)
                    try:
                        imgs = json.loads(r["images_json"] or "[]")
                    except Exception:
                        imgs = []
                    d_csv_rows.append(
                        {
                            "dynamic_id": did,
                            "published_at": pub,
                            "dynamic_type": r["dynamic_type"] or "",
                            "is_forward": r["is_forward"] or 0,
                            "like_count": r["like_count"] if r["like_count"] is not None else "",
                            "comment_count": r["comment_count"] if r["comment_count"] is not None else "",
                            "forward_count": r["forward_count"] if r["forward_count"] is not None else "",
                            "archived_comments": cmt_counts.get((grp, "dynamic", oid), 0),
                            "image_count": len(imgs),
                            "text_preview": clean_preview(r["text_content"], 120),
                            "source_url": f"https://t.bilibili.com/{did}",
                        }
                    )
                catalogs[f"dynamics_{grp}.csv"] = (d_fields, d_csv_rows)
                gstats["dynamics"] = {
                    "count": len(d_rows),
                    "date_min": min(d_dates) if d_dates else None,
                    "date_max": max(d_dates) if d_dates else None,
                    "comments": cmt_totals.get((grp, "dynamic"), {"comments": 0, "carriers": 0}),
                }

            # Articles CSV (if present)
            a_rows = con.execute(
                "SELECT cv_id, opus_id, published_at, title, category, words, "
                "view_count, like_count, reply_count, images_json, summary "
                "FROM articles WHERE target_id = ? "
                "ORDER BY published_at DESC, cv_id DESC",
                (grp,),
            ).fetchall()
            if a_rows:
                a_fields = [
                    "cv_id",
                    "opus_id",
                    "published_at",
                    "title",
                    "category",
                    "words",
                    "view_count",
                    "like_count",
                    "reply_count",
                    "archived_comments",
                    "image_count",
                    "summary_preview",
                    "source_url",
                ]
                a_csv_rows: list[dict] = []
                a_dates: list[str] = []
                for r in a_rows:
                    cv = str(r["cv_id"])
                    pub = r["published_at"] or ""
                    if len(pub) >= 10:
                        a_dates.append(pub[:10])
                    try:
                        imgs = json.loads(r["images_json"] or "[]")
                    except Exception:
                        imgs = []
                    a_csv_rows.append(
                        {
                            "cv_id": cv,
                            "opus_id": r["opus_id"] or "",
                            "published_at": pub,
                            "title": r["title"] or "",
                            "category": r["category"] or "",
                            "words": r["words"] if r["words"] is not None else "",
                            "view_count": r["view_count"] if r["view_count"] is not None else "",
                            "like_count": r["like_count"] if r["like_count"] is not None else "",
                            "reply_count": r["reply_count"] if r["reply_count"] is not None else "",
                            "archived_comments": cmt_counts.get((grp, "article", cv), 0),
                            "image_count": len(imgs),
                            "summary_preview": clean_preview(r["summary"], 120),
                            "source_url": f"https://www.bilibili.com/read/cv{cv}",
                        }
                    )
                catalogs[f"articles_{grp}.csv"] = (a_fields, a_csv_rows)
                gstats["articles"] = {
                    "count": len(a_rows),
                    "date_min": min(a_dates) if a_dates else None,
                    "date_max": max(a_dates) if a_dates else None,
                    "comments": cmt_totals.get((grp, "article"), {"comments": 0, "carriers": 0}),
                }

            gstats["images_files"] = len(gbuckets.get("images", []))
            gstats["total_files"] = sum(len(v) for v in gbuckets.values())
            group_stats[grp] = gstats
    finally:
        con.close()

    dyn_max = (
        group_stats.get("bili_23191782", {}).get("dynamics", {}).get("date_max")
    )
    total_videos = sum(g["videos"]["count"] for g in group_stats.values())
    total_dynamics = sum(
        g.get("dynamics", {}).get("count", 0) for g in group_stats.values()
    )
    total_articles = sum(
        g.get("articles", {}).get("count", 0) for g in group_stats.values()
    )
    total_comments = sum(
        g["videos"]["comments"].get("comments", 0)
        + g.get("dynamics", {}).get("comments", {}).get("comments", 0)
        + g.get("articles", {}).get("comments", {}).get("comments", 0)
        for g in group_stats.values()
    )
    total_images = sum(g["images_files"] for g in group_stats.values())
    total_platform_srt = sum(
        g["videos"]["platform_srt_files"] for g in group_stats.values()
    )

    archive_module_stats = {
        "status": "published",
        "latest_tag": tag,
        "content_date_max": dyn_max,
        "upstream_tool": "https://github.com/chosenecho/biliyt-archiver",
        "total_videos": total_videos,
        "total_dynamics": total_dynamics,
        "total_articles": total_articles,
        "total_comments": total_comments,
        "total_images": total_images,
        "total_platform_srt_files": total_platform_srt,
        "groups": group_stats,
    }

    stats_path = REPO_ROOT / "catalog" / "stats.json"
    existing_stats: dict = {}
    if stats_path.is_file():
        try:
            existing_stats = json.loads(stats_path.read_text(encoding="utf-8"))
        except Exception:
            existing_stats = {}

    latest_tags = dict(existing_stats.get("latest_tags") or {})
    if "asr" not in latest_tags and existing_stats.get("latest_tag"):
        latest_tags["asr"] = existing_stats["latest_tag"]
    latest_tags["archive"] = tag

    modules = dict(existing_stats.get("modules") or {})
    modules["archive"] = archive_module_stats

    stats = {
        "latest_tags": latest_tags,
        "updated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "creator": {
            "name": "未明子 / wujixuanshi",
            "bilibili": {
                "uid": "23191782",
                "url": "https://space.bilibili.com/23191782",
            },
            "youtube": {
                "handle": "@wujixuanshi",
                "url": "https://www.youtube.com/@wujixuanshi",
            },
        },
        "modules": modules,
    }
    return catalogs, stats


def write_repo_metadata(
    tag: str,
    catalogs: dict[str, tuple[list[str], list[dict]]],
    stats: dict,
    out_dir: Path,
) -> None:
    catalog_dir = REPO_ROOT / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)

    for filename, (fieldnames, rows) in catalogs.items():
        csv_path = catalog_dir / filename
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    (catalog_dir / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    manifest_dest = REPO_ROOT / "manifests" / tag
    manifest_dest.mkdir(parents=True, exist_ok=True)
    for fn in ("MANIFEST.json", "SHA256SUMS.txt"):
        shutil.copy2(out_dir / fn, manifest_dest / fn)


def render_release_notes(
    tag: str, manifest: dict, stats: dict | None = None
) -> str:
    arc_meta = (stats or {}).get("modules", {}).get("archive", {})
    groups_meta = arc_meta.get("groups", {})
    bili = groups_meta.get("bili_23191782", {})
    yt = groups_meta.get("yt_wujixuanshi", {})

    def tier_bytes(tiers: set[str]) -> int:
        return sum(
            a["archive_bytes"] for a in manifest["assets"] if a.get("tier") in tiers
        )

    light_bytes = tier_bytes({"light"})
    media_bytes = tier_bytes({"light", "images"})
    raw_bytes = tier_bytes({"light", "raw"})
    full_bytes = manifest["total_archive_bytes"]

    m_range = re.match(r"^archive-base-(\d{4}\.\d{2}\.\d{2})-(\d{4}\.\d{2}\.\d{2})", tag)
    span = (
        f"{m_range.group(1).replace('.', '-')} ~ {m_range.group(2).replace('.', '-')}"
        if m_range
        else tag
    )
    lines = [
        f"## [归档 · 全量] {span}",
        "",
        f"- **Tag**：`{tag}`",
        "- **类型**：全量基线",
        f"- **时间跨度**：`{span}`",
        "- **内容说明**：创作者双平台（Bilibili [`UID 23191782`](https://space.bilibili.com/23191782) 与 YouTube [`@wujixuanshi`](https://www.youtube.com/@wujixuanshi)）的 UP 主档案、动态、专栏、视频元数据、精选评论区、平台自带字幕、原图与封面，以及合并脱敏的 `biliyt.db`（含 FTS5 全文检索索引，由 [`biliyt-archiver`](https://github.com/chosenecho/biliyt-archiver) 生成）。",
        "",
        "> 增量归档在 `archive-delta-*` 发布流维护；Qwen3-ASR 语音转录在 `asr-base-*` / `asr-delta-*` 发布流维护；原始 `.mp4` 视频不托管于 GitHub。",
        "",
    ]

    if bili or yt:
        lines.extend(
            [
                "### 覆盖统计",
                "",
                "| 平台分组 | 视频元数据 | 平台自带 `.srt` | 动态 | 专栏 | 精选评论 | 图片/封面 |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        if bili:
            b_cmts = (
                bili["videos"]["comments"].get("comments", 0)
                + bili.get("dynamics", {}).get("comments", {}).get("comments", 0)
                + bili.get("articles", {}).get("comments", {}).get("comments", 0)
            )
            lines.append(
                f"| `bili_23191782` (Bilibili) | {bili['videos']['count']:,} | "
                f"{bili['videos']['platform_srt_files']:,} | "
                f"{bili.get('dynamics', {}).get('count', 0):,} | "
                f"{bili.get('articles', {}).get('count', 0):,} | "
                f"{b_cmts:,} | {bili['images_files']:,} |"
            )
        if yt:
            lines.append(
                f"| `yt_wujixuanshi` (YouTube) | {yt['videos']['count']:,} | "
                f"{yt['videos']['platform_srt_files']:,} | 0 | 0 | 0 | "
                f"{yt['images_files']:,} |"
            )
        lines.append("")

    lines.extend(
        [
            "### 快速下载与还原",
            "",
            "```bash",
            f"# 1. 轻量阅读与检索包：Markdown 可读稿 + 档案 + 脱敏 SQLite FTS5 库（约 {format_size(light_bytes)}）",
            f"python scripts/restore.py --tag {tag} --preset light",
            "",
            f"# 2. 图文完整包：在轻量包基础上额外包含动态/专栏原图与视频封面（约 {format_size(media_bytes)}）",
            f"python scripts/restore.py --tag {tag} --preset media",
            "",
            f"# 3. 原始数据与平台字幕包：在轻量包基础上包含原始 .json 与平台自带 .srt（约 {format_size(raw_bytes)}）",
            f"python scripts/restore.py --tag {tag} --preset raw",
            "",
            f"# 4. 全量归档包：含全部文本、数据库、图片、原始 JSON 与平台字幕（约 {format_size(full_bytes)}）",
            f"python scripts/restore.py --tag {tag} --preset full",
            "```",
            "",
            "### 资产清单",
            "",
            "| Asset | Group | Tier | Type | Files | Raw Size | Archive Size |",
            "|---|---|---|---|---:|---:|---:|",
        ]
    )

    for a in manifest["assets"]:
        lines.append(
            f"| `{a['name']}` | `{a['group']}` | `{a['tier']}` | {a['kind']} | "
            f"{a['files']:,} | {format_size(a['raw_bytes'])} | {format_size(a['archive_bytes'])} |"
        )

    lines.extend(
        [
            "",
            f"**总计**：{len(manifest['assets'])} 个归档分包 | "
            f"原始体积 {format_size(manifest['total_raw_bytes'])} → "
            f"压缩体积 {format_size(manifest['total_archive_bytes'])}",
            "",
            "### 完整性校验",
            "",
            "```bash",
            "sha256sum -c SHA256SUMS.txt",
            "```",
            "",
            "完整在线目录索引见仓库内 [`catalog/`](https://github.com/chosenecho/weimingzi_archive/tree/main/catalog)，"
            "内容权利声明见 [`CONTENT-RIGHTS.md`](https://github.com/chosenecho/weimingzi_archive/blob/main/CONTENT-RIGHTS.md)。",
        ]
    )
    return "\n".join(lines) + "\n"


def package_archive(
    tag: str,
    biliyt_root: Path,
    videos_root: Path,
    out_dir: Path,
    groups: list[str],
    compresslevel: int = 6,
    max_part_raw_mb: int = DEFAULT_MAX_PART_RAW_MB,
    update_repo_metadata: bool = False,
) -> tuple[dict, dict]:
    started = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)
    max_part_raw_bytes = max_part_raw_mb * 1024 * 1024 if max_part_raw_mb > 0 else 0

    print(
        f"Packaging Archive release {tag} | groups={', '.join(groups)} "
        f"-> {out_dir} (max_part_raw={max_part_raw_mb} MB)"
    )

    collected_files: dict[str, dict[str, Path]] = {}
    buckets_by_group: dict[str, dict[str, list[tuple[Path, str]]]] = {}
    for grp in groups:
        fmap = collect_group_files(grp, biliyt_root, videos_root)
        collected_files[grp] = fmap
        buckets_by_group[grp] = classify_group_files(grp, fmap)

    assets: list[dict] = []

    def add_bucket_archives(
        grp: str,
        bucket_key: str,
        name_prefix: str,
        tier: str,
        kind: str,
        level: int,
        allow_split: bool = True,
    ) -> None:
        entries = buckets_by_group.get(grp, {}).get(bucket_key, [])
        if not entries:
            return
        parts = (
            split_entries_by_raw_size(entries, max_part_raw_bytes)
            if allow_split
            else [entries]
        )
        for idx, part_entries in enumerate(parts, 1):
            t0 = time.time()
            arc_name = (
                f"{name_prefix}.tar.gz"
                if len(parts) == 1
                else f"{name_prefix}-part{idx:02d}.tar.gz"
            )
            arc_path = out_dir / arc_name
            fcnt, rbytes = build_tar_gz(arc_path, part_entries, compresslevel=level)
            abytes = arc_path.stat().st_size
            assets.append(
                {
                    "name": arc_name,
                    "module": "archive",
                    "group": grp,
                    "tier": tier,
                    "kind": kind,
                    "files": fcnt,
                    "raw_bytes": rbytes,
                    "archive_bytes": abytes,
                    "sha256": sha256_file(arc_path),
                }
            )
            print(
                f"  [OK] {arc_name}: {fcnt:,} files, "
                f"{format_size(rbytes)} -> {format_size(abytes)} ({time.time() - t0:.1f}s)"
            )

    for grp in groups:
        if grp == "bili_23191782":
            add_bucket_archives(
                grp, "md", f"archive-{grp}-md", "light", "markdown+profile", compresslevel, allow_split=False
            )
            add_bucket_archives(
                grp, "images", f"archive-{grp}-images", "images", "images", level=1, allow_split=True
            )
            add_bucket_archives(
                grp, "meta-json", f"archive-{grp}-meta-json", "raw", "meta-json", compresslevel, allow_split=True
            )
            add_bucket_archives(
                grp, "platform-srt", f"archive-{grp}-platform-srt", "raw", "platform-srt", compresslevel, allow_split=True
            )
            add_bucket_archives(
                grp, "comments-json", f"archive-{grp}-comments-json", "raw", "comments-json", compresslevel, allow_split=True
            )
        elif grp == "yt_wujixuanshi":
            add_bucket_archives(
                grp, "md", f"archive-{grp}-md", "light", "markdown", compresslevel, allow_split=False
            )
            add_bucket_archives(
                grp, "images", f"archive-{grp}-images", "images", "images", level=1, allow_split=True
            )
            add_bucket_archives(
                grp, "raw", f"archive-{grp}-raw", "raw", "meta-json+platform-srt", compresslevel, allow_split=True
            )

    # Merge & sanitize SQLite database + build catalogs
    biliyt_db = biliyt_root / "state" / "biliyt.db"
    videos_db = videos_root / "_state_videos" / "biliyt.db"
    with tempfile.TemporaryDirectory(prefix="wmz_archive_db_") as tmpdir:
        merged_db = Path(tmpdir) / "biliyt.db"
        t0 = time.time()
        db_summary = build_merged_sanitized_db(
            biliyt_db=biliyt_db,
            videos_db=videos_db,
            dest_db=merged_db,
            groups=groups,
            collected_files=collected_files,
        )
        print(
            f"  [OK] Merged sanitized DB: {db_summary} "
            f"({format_size(merged_db.stat().st_size)}, {time.time() - t0:.1f}s)"
        )

        t0 = time.time()
        db_arc_name = "archive-biliyt-db.tar.gz"
        db_arc_path = out_dir / db_arc_name
        fcnt, rbytes = build_tar_gz(
            db_arc_path, [(merged_db, "data/db/biliyt.db")], compresslevel=compresslevel
        )
        abytes = db_arc_path.stat().st_size
        assets.append(
            {
                "name": db_arc_name,
                "module": "archive",
                "group": "_meta",
                "tier": "light",
                "kind": "sqlite-db",
                "files": fcnt,
                "raw_bytes": rbytes,
                "archive_bytes": abytes,
                "sha256": sha256_file(db_arc_path),
            }
        )
        print(
            f"  [OK] {db_arc_name}: {fcnt} file, "
            f"{format_size(rbytes)} -> {format_size(abytes)} ({time.time() - t0:.1f}s)"
        )

        catalogs, stats = build_catalogs_and_stats(
            merged_db=merged_db,
            groups=groups,
            collected_files=collected_files,
            buckets_by_group=buckets_by_group,
            tag=tag,
        )

    oversized = [a for a in assets if a["archive_bytes"] >= GITHUB_ASSET_LIMIT_BYTES]
    if oversized:
        names = ", ".join(
            f"{a['name']} ({format_size(a['archive_bytes'])})" for a in oversized
        )
        raise RuntimeError(f"Assets exceed GitHub 2 GiB limit: {names}")

    manifest = {
        "schema_version": 1,
        "tag": tag,
        "repo": DEFAULT_REPO,
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

    sums_lines = [f"{a['sha256']}  {a['name']}" for a in assets]
    sums_lines.append(f"{sha256_file(manifest_path)}  {manifest_path.name}")
    sums_path = out_dir / "SHA256SUMS.txt"
    sums_path.write_text("\n".join(sums_lines) + "\n", encoding="utf-8")

    notes_path = out_dir / "RELEASE_NOTES.md"
    notes_path.write_text(
        render_release_notes(tag, manifest, stats), encoding="utf-8"
    )

    if update_repo_metadata:
        write_repo_metadata(tag, catalogs, stats, out_dir)
        print(f"Updated repo metadata: catalog/ ({len(catalogs)} CSVs + stats.json) and manifests/{tag}/")

    print(
        f"Done in {time.time() - started:.1f}s | {len(assets)} archives | "
        f"raw={format_size(manifest['total_raw_bytes'])} -> "
        f"packed={format_size(manifest['total_archive_bytes'])}"
    )
    return manifest, stats


def run_cmd_with_retry(cmd: list[str], retries: int = 4, label: str = "") -> None:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            subprocess.run(cmd, check=True)
            return
        except subprocess.CalledProcessError as e:
            last_err = e
            if attempt < retries:
                wait = 3 * attempt
                print(
                    f"  [WARN] {label or 'command'} failed (attempt {attempt}/{retries}), "
                    f"retrying in {wait}s...",
                    file=sys.stderr,
                )
                time.sleep(wait)
    assert last_err is not None
    raise last_err


def publish_release(
    out_dir: Path,
    tag: str,
    repo: str,
    title: str | None,
    manifest: dict,
    draft: bool = False,
) -> None:
    if not shutil.which("gh"):
        raise RuntimeError("GitHub CLI `gh` not found in PATH.")

    notes_path = out_dir / "RELEASE_NOTES.md"
    if not notes_path.is_file():
        notes_path.write_text(render_release_notes(tag, manifest), encoding="utf-8")

    m_range = re.match(r"^archive-base-(\d{4}\.\d{2}\.\d{2})-(\d{4}\.\d{2}\.\d{2})", tag)
    if m_range:
        span = f"{m_range.group(1).replace('.', '-')} ~ {m_range.group(2).replace('.', '-')}"
        default_title = f"[归档 · 全量] {span}"
    else:
        default_title = f"[归档 · 全量] {tag}"
    release_title = title or default_title

    probe = subprocess.run(
        ["gh", "release", "view", tag, "-R", repo],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode != 0:
        create_cmd = [
            "gh",
            "release",
            "create",
            tag,
            "-R",
            repo,
            "--title",
            release_title,
            "--notes-file",
            str(notes_path),
            str(out_dir / "MANIFEST.json"),
            str(out_dir / "SHA256SUMS.txt"),
        ]
        if draft:
            create_cmd.append("--draft")
        print(f"Creating GitHub Release {tag} on {repo}...")
        run_cmd_with_retry(create_cmd, label=f"create release {tag}")
    else:
        print(f"Release {tag} already exists on {repo}; updating notes & metadata...")
        run_cmd_with_retry(
            [
                "gh",
                "release",
                "edit",
                tag,
                "-R",
                repo,
                "--title",
                release_title,
                "--notes-file",
                str(notes_path),
            ],
            label=f"edit release {tag}",
        )
        run_cmd_with_retry(
            [
                "gh",
                "release",
                "upload",
                tag,
                "-R",
                repo,
                "--clobber",
                str(out_dir / "MANIFEST.json"),
                str(out_dir / "SHA256SUMS.txt"),
            ],
            label="upload MANIFEST/SHA256SUMS",
        )

    assets = manifest["assets"]
    for idx, asset in enumerate(assets, 1):
        arc_path = out_dir / asset["name"]
        if not arc_path.is_file():
            raise FileNotFoundError(f"Expected staged asset missing: {arc_path}")
        print(
            f"  [{idx}/{len(assets)}] Uploading {asset['name']} "
            f"({format_size(asset['archive_bytes'])})..."
        )
        run_cmd_with_retry(
            [
                "gh",
                "release",
                "upload",
                tag,
                "-R",
                repo,
                "--clobber",
                str(arc_path),
            ],
            label=f"upload {asset['name']}",
        )
    print(f"Published {tag} -> https://github.com/{repo}/releases/tag/{tag}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Package and publish biliyt-archiver snapshots for weimingzi_archive."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--tag",
            default="auto",
            help="Release tag (archive-vYYYY.MM.DD or 'auto' to use latest dynamic date)",
        )
        p.add_argument(
            "--allow-custom-tag",
            action="store_true",
            help="Bypass archive-vYYYY.MM.DD tag validation",
        )
        p.add_argument(
            "--biliyt-root",
            type=Path,
            default=DEFAULT_BILIYT_ROOT,
            help=f"Path to biliyt-archiver workspace (default: {DEFAULT_BILIYT_ROOT})",
        )
        p.add_argument(
            "--videos-root",
            type=Path,
            default=DEFAULT_VIDEOS_ROOT,
            help=f"Path to historical videos/comments root (default: {DEFAULT_VIDEOS_ROOT})",
        )
        p.add_argument(
            "--out-dir",
            type=Path,
            default=None,
            help="Staging output directory (default: release_dist/<tag>)",
        )
        p.add_argument(
            "--groups",
            nargs="+",
            help=f"Subset of allowed groups (whitelist: {', '.join(ALLOWED_GROUPS)})",
        )
        p.add_argument("--level", type=int, default=6, choices=range(1, 10))
        p.add_argument(
            "--max-part-raw-mb",
            type=int,
            default=DEFAULT_MAX_PART_RAW_MB,
            help=f"Max raw MB per split part archive (default: {DEFAULT_MAX_PART_RAW_MB}, 0=disable)",
        )
        p.add_argument(
            "--update-repo-metadata",
            action="store_true",
            help="Sync catalog/*.csv, catalog/stats.json, and manifests/<tag>/ in repo",
        )

    p_pack = sub.add_parser("package", help="Package archive release assets locally")
    add_common_args(p_pack)

    p_pub = sub.add_parser("publish", help="Package (optional) and publish GitHub Release")
    add_common_args(p_pub)
    p_pub.add_argument("--repo", default=DEFAULT_REPO)
    p_pub.add_argument("--title", help="Custom GitHub Release title")
    p_pub.add_argument("--draft", action="store_true")
    p_pub.add_argument(
        "--skip-package",
        action="store_true",
        help="Upload existing staged assets from --out-dir without re-packing",
    )
    p_pub.add_argument(
        "--clean",
        action="store_true",
        help="Remove staging directory after successful publish",
    )

    args = parser.parse_args()
    tag = (
        detect_latest_dynamic_tag(args.biliyt_root)
        if args.tag == "auto"
        else validate_tag(args.tag, allow_custom=args.allow_custom_tag)
    )
    out_dir = args.out_dir or (DEFAULT_DIST_ROOT / tag)
    groups = validate_groups(args.groups)

    if args.cmd == "package":
        package_archive(
            tag=tag,
            biliyt_root=args.biliyt_root,
            videos_root=args.videos_root,
            out_dir=out_dir,
            groups=groups,
            compresslevel=args.level,
            max_part_raw_mb=args.max_part_raw_mb,
            update_repo_metadata=args.update_repo_metadata,
        )
        return 0

    if args.cmd == "publish":
        if args.skip_package:
            manifest_path = out_dir / "MANIFEST.json"
            if not manifest_path.is_file():
                raise FileNotFoundError(
                    f"Missing {manifest_path}; run `package` first or omit --skip-package."
                )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            manifest, _ = package_archive(
                tag=tag,
                biliyt_root=args.biliyt_root,
                videos_root=args.videos_root,
                out_dir=out_dir,
                groups=groups,
                compresslevel=args.level,
                max_part_raw_mb=args.max_part_raw_mb,
                update_repo_metadata=args.update_repo_metadata,
            )

        publish_release(
            out_dir=out_dir,
            tag=tag,
            repo=args.repo,
            title=args.title,
            manifest=manifest,
            draft=args.draft,
        )
        if args.clean:
            shutil.rmtree(out_dir, ignore_errors=True)
            print(f"Cleaned staging directory: {out_dir}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
