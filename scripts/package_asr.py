#!/usr/bin/env python3
"""Package and publish ASR transcript release snapshots for weimingzi_archive.

Pure Python standard library implementation.
Enforces strict creator whitelist (bili_23191782, yt_wujixuanshi), sanitizes
local paths and private endpoints from pipeline.db, generates UTF-8 PAX
.tar.gz release bundles (< 2 GiB each), updates repo catalog/ and manifests/,
and publishes to GitHub Releases via `gh`.
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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "chosenecho/weimingzi_archive"
DEFAULT_ASR_ROOT = Path(r"C:\WorkSpace\asr")
DEFAULT_DIST_ROOT = REPO_ROOT / "release_dist"
DEFAULT_MAX_PART_RAW_MB = 450  # ~80-90 MB compressed for JSON, safe for GitHub uploads
GITHUB_ASSET_LIMIT_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB hard GitHub limit

ALLOWED_GROUPS: tuple[str, ...] = ("bili_23191782", "yt_wujixuanshi")
TAG_RE = re.compile(
    r"^asr-(?:base-\d{4}\.\d{2}\.\d{2}-\d{4}\.\d{2}\.\d{2}|v\d{4}\.\d{2}\.\d{2})(-r\d+)?$"
)
DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*-\s*")
BILI_PART_RE = re.compile(r"^(BV[0-9A-Za-z]+)_p(\d+)$")

FORBIDDEN_SANITIZE_TOKENS = (
    "C:\\",
    "D:\\",
    "c:\\",
    "d:\\",
    "modal.run",
    "127.0.0.1",
    "localhost:",
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


def detect_latest_asr_tag(transcripts_dir: Path, groups: list[str]) -> str:
    dates: list[str] = []
    for grp in groups:
        gdir = transcripts_dir / grp
        if not gdir.is_dir():
            continue
        for p in gdir.glob("*.txt"):
            m = DATE_PREFIX_RE.match(p.name)
            if m:
                dates.append(m.group(1))
    if not dates:
        raise RuntimeError(
            f"Could not detect .txt content dates under {transcripts_dir}"
        )
    return f"asr-base-{min(dates).replace('-', '.')}-{max(dates).replace('-', '.')}"


def validate_tag(tag: str, allow_custom: bool = False) -> str:
    if not allow_custom and not TAG_RE.match(tag):
        raise ValueError(
            f"Tag '{tag}' must follow asr-base-YYYY.MM.DD-YYYY.MM.DD (or legacy asr-vYYYY.MM.DD). "
            "Pass --allow-custom-tag to override."
        )
    return tag


def validate_groups(requested: list[str] | None, transcripts_dir: Path) -> list[str]:
    if requested:
        invalid = [g for g in requested if g not in ALLOWED_GROUPS]
        if invalid:
            raise ValueError(
                f"Disallowed source groups: {invalid}. Allowed whitelist: {list(ALLOWED_GROUPS)}"
            )
        groups = [g for g in ALLOWED_GROUPS if g in requested]
    else:
        groups = [g for g in ALLOWED_GROUPS if (transcripts_dir / g).is_dir()]
    if not groups:
        raise FileNotFoundError(
            f"No allowed transcript group directories found under {transcripts_dir}"
        )
    return groups


def make_source_url(group: str, video_id: str) -> str:
    if group == "bili_23191782":
        m = BILI_PART_RE.match(video_id)
        if m:
            return f"https://www.bilibili.com/video/{m.group(1)}?p={m.group(2)}"
        return f"https://www.bilibili.com/video/{video_id}"
    if group == "yt_wujixuanshi":
        return f"https://www.youtube.com/watch?v={video_id}"
    return ""


def normalize_transcript_rel_path(
    raw_path: str | None, group: str, group_dir: Path
) -> str | None:
    if not raw_path:
        return None
    name = Path(raw_path.replace("\\", "/")).name
    if not name:
        return None
    candidate = group_dir / name
    if candidate.is_file():
        return f"data/transcripts/{group}/{name}"
    return None


def extract_upload_date(video_rel_path: str | None, title: str | None) -> str:
    for text in (video_rel_path or "", title or ""):
        m = DATE_PREFIX_RE.match(text)
        if m:
            return m.group(1)
    return ""


def build_tar_gz(
    archive_path: Path,
    files: Iterable[tuple[Path, str]],
    compresslevel: int = 6,
) -> tuple[int, int]:
    """Write a UTF-8 PAX .tar.gz archive; returns (file_count, raw_bytes)."""
    count = 0
    raw_bytes = 0
    tmp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
    try:
        with tarfile.open(
            tmp_path,
            mode="w:gz",
            format=tarfile.PAX_FORMAT,
            compresslevel=compresslevel,
        ) as tf:
            for src, arcname in files:
                tf.add(src, arcname=arcname, recursive=False)
                count += 1
                raw_bytes += src.stat().st_size
        tmp_path.replace(archive_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return count, raw_bytes


def collect_group_files(
    transcripts_dir: Path, group: str, exts: set[str]
) -> list[tuple[Path, str]]:
    group_dir = transcripts_dir / group
    if not group_dir.is_dir():
        return []
    result: list[tuple[Path, str]] = []
    for p in sorted(group_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in exts:
            rel_inside = p.relative_to(group_dir).as_posix()
            arcname = f"data/transcripts/{group}/{rel_inside}"
            result.append((p, arcname))
    return result


def partition_files(
    files: list[tuple[Path, str]],
    max_raw_bytes: int,
) -> list[list[tuple[Path, str]]]:
    """Split files into standalone chunks so each archive stays upload-safe."""
    if not files:
        return []
    sized = [(src, arc, src.stat().st_size) for src, arc in files]
    total_raw = sum(sz for _, _, sz in sized)
    if max_raw_bytes <= 0 or total_raw <= max_raw_bytes:
        return [files]

    chunks: list[list[tuple[Path, str]]] = []
    cur: list[tuple[Path, str]] = []
    cur_bytes = 0
    for src, arc, sz in sized:
        if cur and cur_bytes + sz > max_raw_bytes:
            chunks.append(cur)
            cur = []
            cur_bytes = 0
        cur.append((src, arc))
        cur_bytes += sz
    if cur:
        chunks.append(cur)
    return chunks


def export_sanitized_db(
    src_db: Path,
    dest_db: Path,
    transcripts_dir: Path,
    groups: list[str],
) -> None:
    """Create a WAL-merged, whitelist-filtered, path-normalized, sanitized SQLite DB."""
    if dest_db.exists():
        dest_db.unlink()

    src_conn = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(str(dest_db))
    try:
        src_conn.backup(dst_conn)
        dst_conn.commit()

        # 1. Strict whitelist filtering on media_items & pipeline_runs
        placeholders = ",".join("?" for _ in groups)
        dst_conn.execute(
            f"DELETE FROM media_items WHERE source_group NOT IN ({placeholders})",
            groups,
        )
        tables = {
            r[0]
            for r in dst_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "pipeline_runs" in tables:
            dst_conn.execute(
                f"DELETE FROM pipeline_runs WHERE source_group IS NOT NULL "
                f"AND source_group NOT IN ({placeholders})",
                groups,
            )
            run_cols = {
                r[1]
                for r in dst_conn.execute("PRAGMA table_info(pipeline_runs)").fetchall()
            }
            for col in ("config_json", "params_json"):
                if col in run_cols:
                    dst_conn.execute(
                        f"UPDATE pipeline_runs SET {col} = NULL WHERE {col} IS NOT NULL"
                    )

        # 2. Sanitize local absolute paths & private server URLs in media_items
        rows = dst_conn.execute(
            "SELECT id, source_group, video_rel_path, txt_path, srt_path, json_path "
            "FROM media_items"
        ).fetchall()
        updates = []
        for row_id, grp, video_rel_path, txt_path, srt_path, json_path in rows:
            group_dir = transcripts_dir / grp
            norm_video = f"videos/{grp}/{Path(video_rel_path.replace(chr(92), '/')).name}"
            norm_txt = normalize_transcript_rel_path(txt_path, grp, group_dir)
            norm_srt = normalize_transcript_rel_path(srt_path, grp, group_dir)
            norm_json = normalize_transcript_rel_path(json_path, grp, group_dir)
            updates.append((norm_video, norm_txt, norm_srt, norm_json, row_id))

        dst_conn.executemany(
            "UPDATE media_items "
            "SET video_path = ?, audio_path = NULL, asr_server_url = NULL, "
            "    txt_path = ?, srt_path = ?, json_path = ? "
            "WHERE id = ?",
            updates,
        )
        dst_conn.commit()

        # 3. Verify whitelist and sanitization invariants
        found_groups = {
            r[0]
            for r in dst_conn.execute(
                "SELECT DISTINCT source_group FROM media_items"
            ).fetchall()
        }
        if not found_groups.issubset(set(ALLOWED_GROUPS)):
            raise RuntimeError(
                f"Whitelist violation in exported DB: {found_groups - set(ALLOWED_GROUPS)}"
            )

        for tbl in ("media_items", "pipeline_runs"):
            if tbl not in tables:
                continue
            for row in dst_conn.execute(f"SELECT * FROM {tbl}").fetchall():
                for val in row:
                    if isinstance(val, str):
                        for tok in FORBIDDEN_SANITIZE_TOKENS:
                            if tok in val:
                                raise RuntimeError(
                                    f"Sanitization check failed in {tbl}: found '{tok}' in '{val[:120]}'"
                                )

        dst_conn.execute("PRAGMA journal_mode=DELETE")
        dst_conn.execute("VACUUM")
        dst_conn.commit()
    finally:
        dst_conn.close()
        src_conn.close()


def build_catalog_and_stats(
    src_db: Path,
    transcripts_dir: Path,
    groups: list[str],
    tag: str,
) -> tuple[dict[str, list[dict]], dict]:
    """Generate per-group CSV rows and aggregate ASR statistics."""
    conn = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    catalogs: dict[str, list[dict]] = {}
    group_stats: dict[str, dict] = {}
    try:
        for grp in groups:
            group_dir = transcripts_dir / grp
            rows = conn.execute(
                """
                SELECT video_id, title, video_rel_path, duration_sec,
                       txt_path, srt_path, json_path, asr_status, asr_finished_at
                FROM media_items
                WHERE source_group = ?
                ORDER BY video_rel_path ASC, video_id ASC
                """,
                (grp,),
            ).fetchall()

            csv_rows: list[dict] = []
            txt_counter: Counter[str] = Counter()
            dates: list[str] = []
            total_duration = 0.0
            done_count = 0

            for (
                video_id,
                title,
                video_rel_path,
                duration_sec,
                txt_path,
                srt_path,
                json_path,
                asr_status,
                asr_finished_at,
            ) in rows:
                upload_date = extract_upload_date(video_rel_path, title)
                if upload_date:
                    dates.append(upload_date)
                dur = round(float(duration_sec), 2) if duration_sec is not None else ""
                if duration_sec is not None:
                    total_duration += float(duration_sec)
                if asr_status == "done":
                    done_count += 1

                norm_txt = normalize_transcript_rel_path(txt_path, grp, group_dir) or ""
                norm_srt = normalize_transcript_rel_path(srt_path, grp, group_dir) or ""
                norm_json = normalize_transcript_rel_path(json_path, grp, group_dir) or ""
                if norm_txt:
                    txt_counter[norm_txt] += 1

                csv_rows.append(
                    {
                        "video_id": video_id,
                        "upload_date": upload_date,
                        "title": title,
                        "duration_sec": dur,
                        "source_url": make_source_url(grp, video_id),
                        "txt_rel_path": norm_txt,
                        "srt_rel_path": norm_srt,
                        "json_rel_path": norm_json,
                        "asr_status": asr_status,
                        "asr_finished_at": asr_finished_at or "",
                    }
                )

            # Count actual files on disk
            ext_counts: Counter[str] = Counter()
            ext_bytes: Counter[str] = Counter()
            for p in group_dir.rglob("*"):
                if p.is_file():
                    ext = p.suffix.lower()
                    ext_counts[ext] += 1
                    ext_bytes[ext] += p.stat().st_size

            shared_paths = sorted(k for k, v in txt_counter.items() if v > 1)
            catalogs[grp] = csv_rows
            group_stats[grp] = {
                "db_records": len(rows),
                "asr_done_records": done_count,
                "duration_hours": round(total_duration / 3600.0, 2),
                "date_min": min(dates) if dates else None,
                "date_max": max(dates) if dates else None,
                "files": {
                    "txt": ext_counts.get(".txt", 0),
                    "srt": ext_counts.get(".srt", 0),
                    "json": ext_counts.get(".json", 0),
                },
                "raw_bytes": {
                    "txt": ext_bytes.get(".txt", 0),
                    "srt": ext_bytes.get(".srt", 0),
                    "json": ext_bytes.get(".json", 0),
                },
                "shared_output_paths": shared_paths,
            }
    finally:
        conn.close()

    all_dates = [g["date_max"] for g in group_stats.values() if g.get("date_max")]
    asr_module_stats = {
        "status": "published",
        "latest_tag": tag,
        "content_date_max": max(all_dates) if all_dates else None,
        "upstream_tool": "https://github.com/chosenecho/qwen3-asr-pipeline",
        "total_db_records": sum(g["db_records"] for g in group_stats.values()),
        "total_txt_files": sum(g["files"]["txt"] for g in group_stats.values()),
        "total_srt_files": sum(g["files"]["srt"] for g in group_stats.values()),
        "total_json_files": sum(g["files"]["json"] for g in group_stats.values()),
        "total_duration_hours": round(
            sum(g["duration_hours"] for g in group_stats.values()), 2
        ),
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
    latest_tags["asr"] = tag
    modules = dict(existing_stats.get("modules") or {})
    modules["asr"] = asr_module_stats
    if "archive" not in modules:
        modules["archive"] = {
            "status": "planned",
            "upstream_tool": "https://github.com/chosenecho/biliyt-archiver",
        }

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
    catalogs: dict[str, list[dict]],
    stats: dict,
    out_dir: Path,
) -> None:
    catalog_dir = REPO_ROOT / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "video_id",
        "upload_date",
        "title",
        "duration_sec",
        "source_url",
        "txt_rel_path",
        "srt_rel_path",
        "json_rel_path",
        "asr_status",
        "asr_finished_at",
    ]
    for grp, rows in catalogs.items():
        csv_path = catalog_dir / f"asr_{grp}.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    (catalog_dir / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    manifest_dest_dir = REPO_ROOT / "manifests" / tag
    manifest_dest_dir.mkdir(parents=True, exist_ok=True)
    for fn in ("MANIFEST.json", "SHA256SUMS.txt"):
        shutil.copy2(out_dir / fn, manifest_dest_dir / fn)


def render_release_notes(tag: str, manifest: dict, stats: dict | None = None) -> str:
    asr_meta = (stats or {}).get("modules", {}).get("asr", {})
    groups_meta = asr_meta.get("groups", {})
    bili = groups_meta.get("bili_23191782", {})
    yt = groups_meta.get("yt_wujixuanshi", {})

    light_bytes = sum(
        a["archive_bytes"] for a in manifest["assets"] if a.get("tier") == "light"
    )
    full_bytes = manifest["total_archive_bytes"]

    m_range = re.match(r"^asr-base-(\d{4}\.\d{2}\.\d{2})-(\d{4}\.\d{2}\.\d{2})", tag)
    span = (
        f"{m_range.group(1).replace('.', '-')} ~ {m_range.group(2).replace('.', '-')}"
        if m_range
        else tag
    )
    lines = [
        f"## [ASR · 全量] {span}",
        "",
        f"- **Tag**：`{tag}`",
        "- **类型**：全量基线",
        f"- **时间跨度**：`{span}`",
        "- **内容说明**：创作者双平台（Bilibili [`UID 23191782`](https://space.bilibili.com/23191782) 与 YouTube [`@wujixuanshi`](https://www.youtube.com/@wujixuanshi)）视频的 Qwen3-ASR 语音转录产物（`.txt` / `.srt` / 词级 `.json`）与脱敏状态库 `asr_pipeline.db`（由 [`qwen3-asr-pipeline`](https://github.com/chosenecho/qwen3-asr-pipeline) 生成）。",
        "",
        "> 增量转录在 `asr-delta-*` 发布流维护；动态、专栏、视频元数据、评论与图片在 `archive-base-*` / `archive-delta-*` 发布流维护。",
        "",
    ]

    if bili or yt:
        lines.extend(
            [
                "### 覆盖统计",
                "",
                "| 平台分组 | 数据库记录 | `.txt` / `.srt` 组数 | 词级 `.json` | 总时长（小时） | 日期跨度 |",
                "|---|---:|---:|---:|---:|---|",
            ]
        )
        if bili:
            lines.append(
                f"| `bili_23191782` (Bilibili) | {bili['db_records']:,} | "
                f"{bili['files']['txt']:,} | {bili['files']['json']:,} | "
                f"{bili['duration_hours']:,.2f} | {bili['date_min']} ~ {bili['date_max']} |"
            )
        if yt:
            lines.append(
                f"| `yt_wujixuanshi` (YouTube) | {yt['db_records']:,} | "
                f"{yt['files']['txt']:,} | {yt['files']['json']:,} | "
                f"{yt['duration_hours']:,.2f} | {yt['date_min']} ~ {yt['date_max']} |"
            )
        lines.append("")
        if bili and bili.get("shared_output_paths"):
            lines.append(
                f"> 说明：`bili_23191782` 有 {bili['db_records']:,} 条记录对应 {bili['files']['txt']:,} 组文件"
                "（上游多 P 视频 `BV19h411Y7bv_p1` / `BV19h411Y7bv_p2` 因文件名含 `2.0` 截断共用同一输出路径，已在 `catalog/asr_bili_23191782.csv` 中如实保留）。"
            )
            lines.append("")

    lines.extend(
        [
            "### 快速下载与还原",
            "",
            "仓库内提供纯 Python 标准库还原脚本（自动下载、校验 SHA-256 并解压至 `data/`）：",
            "",
            "```bash",
            f"# 轻量模式（推荐）：仅下载纯文本 .txt + 字幕 .srt + 脱敏数据库（约 {format_size(light_bytes)}）",
            f"python scripts/restore.py --tag {tag} --preset light",
            "",
            f"# 完整模式：额外包含词级时间戳 .json（总计 {format_size(full_bytes)}）",
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
            "完整目录索引见仓库内 [`catalog/`](https://github.com/chosenecho/weimingzi_archive/tree/main/catalog)，"
            "内容权利声明见 [`CONTENT-RIGHTS.md`](https://github.com/chosenecho/weimingzi_archive/blob/main/CONTENT-RIGHTS.md)。",
        ]
    )
    return "\n".join(lines) + "\n"


def package_asr(
    tag: str,
    asr_root: Path,
    out_dir: Path,
    groups: list[str],
    include_json: bool = True,
    include_db: bool = True,
    compresslevel: int = 6,
    max_part_raw_mb: int = DEFAULT_MAX_PART_RAW_MB,
    update_repo_metadata: bool = False,
) -> tuple[dict, dict | None]:
    transcripts_dir = asr_root / "data" / "transcripts"
    src_db = asr_root / "data" / "pipeline.db"

    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in list(out_dir.glob("*.tar.gz")) + [
        out_dir / "MANIFEST.json",
        out_dir / "SHA256SUMS.txt",
        out_dir / "RELEASE_NOTES.md",
    ]:
        stale.unlink(missing_ok=True)

    max_raw_bytes = max_part_raw_mb * 1024 * 1024 if max_part_raw_mb > 0 else 0
    started = time.time()
    assets: list[dict] = []

    print(
        f"Packaging ASR release {tag} | groups={', '.join(groups)} -> {out_dir} "
        f"(max_part_raw={max_part_raw_mb} MB)"
    )

    def pack_bucket(
        group: str,
        stem: str,
        kind: str,
        tier: str,
        bucket_files: list[tuple[Path, str]],
    ) -> None:
        chunks = partition_files(bucket_files, max_raw_bytes)
        multi = len(chunks) > 1
        for i, chunk in enumerate(chunks, 1):
            arc_name = f"{stem}-part{i:02d}.tar.gz" if multi else f"{stem}.tar.gz"
            arc = out_dir / arc_name
            t0 = time.time()
            cnt, raw_b = build_tar_gz(arc, chunk, compresslevel=compresslevel)
            comp_b = arc.stat().st_size
            digest = sha256_file(arc)
            print(
                f"  [OK] {arc.name}: {cnt:,} files, {format_size(raw_b)} -> "
                f"{format_size(comp_b)} ({time.time() - t0:.1f}s)"
            )
            assets.append(
                {
                    "name": arc.name,
                    "module": "asr",
                    "group": group,
                    "tier": tier,
                    "kind": kind,
                    "files": cnt,
                    "raw_bytes": raw_b,
                    "archive_bytes": comp_b,
                    "sha256": digest,
                }
            )

    for grp in groups:
        text_files = collect_group_files(transcripts_dir, grp, {".txt", ".srt"})
        if text_files:
            pack_bucket(grp, f"asr-{grp}-text", "text+srt", "light", text_files)
        if include_json:
            json_files = collect_group_files(transcripts_dir, grp, {".json"})
            if json_files:
                pack_bucket(grp, f"asr-{grp}-json", "word-json", "full", json_files)

    if include_db and src_db.is_file():
        with tempfile.TemporaryDirectory(prefix="wmz_asr_db_") as tmp:
            clean_db = Path(tmp) / "asr_pipeline.db"
            export_sanitized_db(src_db, clean_db, transcripts_dir, groups)
            arc = out_dir / "asr-pipeline-db.tar.gz"
            t0 = time.time()
            cnt, raw_b = build_tar_gz(
                arc,
                [(clean_db, "data/db/asr_pipeline.db")],
                compresslevel=compresslevel,
            )
            comp_b = arc.stat().st_size
            digest = sha256_file(arc)
            print(
                f"  [OK] {arc.name}: {cnt} file, {format_size(raw_b)} -> "
                f"{format_size(comp_b)} ({time.time() - t0:.1f}s)"
            )
            assets.append(
                {
                    "name": arc.name,
                    "module": "asr",
                    "group": "_meta",
                    "tier": "light",
                    "kind": "sqlite-db",
                    "files": cnt,
                    "raw_bytes": raw_b,
                    "archive_bytes": comp_b,
                    "sha256": digest,
                }
            )

    oversized = [a for a in assets if a["archive_bytes"] >= GITHUB_ASSET_LIMIT_BYTES]
    if oversized:
        names = ", ".join(
            f"{a['name']} ({format_size(a['archive_bytes'])})" for a in oversized
        )
        raise RuntimeError(f"Assets exceed GitHub 2 GiB limit: {names}")

    catalogs: dict[str, list[dict]] = {}
    stats: dict | None = None
    if src_db.is_file():
        catalogs, stats = build_catalog_and_stats(src_db, transcripts_dir, groups, tag)

    manifest = {
        "schema_version": 1,
        "tag": tag,
        "repo": DEFAULT_REPO,
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

    sums_lines = [f"{a['sha256']}  {a['name']}" for a in assets]
    sums_lines.append(f"{sha256_file(manifest_path)}  {manifest_path.name}")
    sums_path = out_dir / "SHA256SUMS.txt"
    sums_path.write_text("\n".join(sums_lines) + "\n", encoding="utf-8")

    notes_path = out_dir / "RELEASE_NOTES.md"
    notes_path.write_text(render_release_notes(tag, manifest, stats), encoding="utf-8")

    if update_repo_metadata and stats is not None:
        write_repo_metadata(tag, catalogs, stats, out_dir)
        print(f"Updated repo metadata: catalog/ and manifests/{tag}/")

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

    m_range = re.match(r"^asr-base-(\d{4}\.\d{2}\.\d{2})-(\d{4}\.\d{2}\.\d{2})", tag)
    if m_range:
        span = f"{m_range.group(1).replace('.', '-')} ~ {m_range.group(2).replace('.', '-')}"
        default_title = f"[ASR · 全量] {span}"
    else:
        default_title = f"[ASR · 全量] {tag}"
    release_title = title or default_title

    # Check if release already exists
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
                str(out_dir / "MANIFEST.json"),
                str(out_dir / "SHA256SUMS.txt"),
                "-R",
                repo,
                "--clobber",
            ],
            label="upload manifest & checksums",
        )

    # Upload archives sequentially with retries for stability
    for idx, asset in enumerate(manifest["assets"], 1):
        arc_path = out_dir / asset["name"]
        if not arc_path.is_file():
            raise FileNotFoundError(f"Missing packaged asset: {arc_path}")
        print(
            f"  [{idx}/{len(manifest['assets'])}] Uploading {arc_path.name} "
            f"({format_size(asset['archive_bytes'])})..."
        )
        run_cmd_with_retry(
            [
                "gh",
                "release",
                "upload",
                tag,
                str(arc_path),
                "-R",
                repo,
                "--clobber",
            ],
            label=f"upload {arc_path.name}",
        )
    print(f"Published {tag} -> https://github.com/{repo}/releases/tag/{tag}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Package and publish ASR transcript release snapshots for weimingzi_archive."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--tag",
            required=True,
            help="Date-based release tag, e.g. v2026.11.27",
        )
        p.add_argument(
            "--allow-custom-tag",
            action="store_true",
            help="Allow tag format outside vYYYY.MM.DD[-rN]",
        )
        p.add_argument(
            "--asr-root",
            type=Path,
            default=DEFAULT_ASR_ROOT,
            help=f"Upstream qwen3-asr-pipeline root directory (default: {DEFAULT_ASR_ROOT})",
        )
        p.add_argument(
            "--out-dir",
            type=Path,
            default=None,
            help="Staging directory (default: release_dist/<tag>)",
        )
        p.add_argument(
            "--groups",
            nargs="+",
            help=f"Subset of allowed groups (whitelist: {', '.join(ALLOWED_GROUPS)})",
        )
        p.add_argument("--no-json", action="store_true", help="Exclude word-level .json archives")
        p.add_argument("--no-db", action="store_true", help="Exclude asr_pipeline.db archive")
        p.add_argument("--level", type=int, default=6, choices=range(1, 10))
        p.add_argument(
            "--max-part-raw-mb",
            type=int,
            default=DEFAULT_MAX_PART_RAW_MB,
            help=f"Max raw MB per part archive (default: {DEFAULT_MAX_PART_RAW_MB}, 0=disable)",
        )
        p.add_argument(
            "--update-repo-metadata",
            action="store_true",
            help="Sync catalog/*.csv, catalog/stats.json, and manifests/<tag>/ in repo",
        )

    p_pack = sub.add_parser("package", help="Package ASR release assets locally")
    add_common_args(p_pack)

    p_pub = sub.add_parser("publish", help="Package (optional) and publish GitHub Release")
    add_common_args(p_pub)
    p_pub.add_argument("--repo", default=DEFAULT_REPO, help=f"GitHub repo (default: {DEFAULT_REPO})")
    p_pub.add_argument("--title", help="Custom GitHub Release title")
    p_pub.add_argument("--draft", action="store_true", help="Create as draft release")
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
    if args.tag == "auto":
        detected_groups = validate_groups(args.groups, args.asr_root / "data" / "transcripts")
        tag = detect_latest_asr_tag(args.asr_root / "data" / "transcripts", detected_groups)
    else:
        tag = validate_tag(args.tag, allow_custom=args.allow_custom_tag)
    out_dir = args.out_dir or (DEFAULT_DIST_ROOT / tag)

    if args.cmd == "package":
        groups = validate_groups(args.groups, args.asr_root / "data" / "transcripts")
        package_asr(
            tag=tag,
            asr_root=args.asr_root,
            out_dir=out_dir,
            groups=groups,
            include_json=not args.no_json,
            include_db=not args.no_db,
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
            groups = validate_groups(args.groups, args.asr_root / "data" / "transcripts")
            manifest, _ = package_asr(
                tag=tag,
                asr_root=args.asr_root,
                out_dir=out_dir,
                groups=groups,
                include_json=not args.no_json,
                include_db=not args.no_db,
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
