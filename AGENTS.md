# AGENTS.md

`weimingzi_archive` is a lightweight Git index + GitHub Releases archive for creator **未明子** (`bili_23191782` on Bilibili, `yt_wujixuanshi` on YouTube). All scripts use Python 3.9+ stdlib only; `gh` CLI is required only for publishing Releases.

## Context Pointers

- Changing directory layout, tier names, or SQLite/CSV schemas: read `docs/LAYOUT.md`.
- Packaging, publishing, or migrating a release snapshot: read `docs/MAINTENANCE.md`.
- Checking latest counts, tags, or date spans: read `catalog/stats.json` (single source of truth).
- Answering copyright, licensing, or takedown questions: read `CONTENT-RIGHTS.md` and `LICENSE`.

## Core Invariants

- **Dual independent release streams (+ time-range deltas)**: base snapshots `asr-vYYYY.MM.DD` and `archive-vYYYY.MM.DD` are released independently, with optional lightweight incremental releases `<module>-delta-YYYY.MM.DD-YYYY.MM.DD` whose SQLite files unpack to `data/db/deltas/` and auto-merge in `restore.py`. Keep their tags and manifests separate.
- **Strict creator whitelist**: only `bili_23191782` and `yt_wujixuanshi` may appear in `catalog/`, `manifests/`, SQLite DBs, or Release assets.
- **Lightweight Git main**: commit only text catalogs, manifests, docs, and scripts; keep `data/`, `release_dist/`, `.tar.gz`, `.db`, and raw media binaries out of Git.
- **Sanitized exports**: strip local absolute paths (`C:\`, `D:\`), usernames, cookies, private server URLs, and `sync_runs` rows before publishing. Exclude `.mp4`/`.mkv`/`.m4a` and other raw media binaries from Release archives.
- **GitHub asset cap**: keep every `.tar.gz` under 2 GiB (scripts split large tiers at 450 MB raw by default) and ship `MANIFEST.json` + `SHA256SUMS.txt`.

## Commands

```bash
# Syntax-check all scripts
python -m py_compile scripts/package_asr.py scripts/package_archive.py scripts/package_delta.py scripts/restore.py

# Inspect CLI flags (authoritative source)
python scripts/restore.py --help
python scripts/package_delta.py --help
python scripts/package_asr.py --help
python scripts/package_archive.py --help

# Verify live release resolution without downloading
python scripts/restore.py --preset full --list

# Smoke-test online download, SHA-256 verification, and extraction
python scripts/restore.py --modules asr --preset light --groups yt_wujixuanshi --dest ./_verify_tmp
```

## Non-Obvious Gotchas

- `data/db/biliyt.db` is merged from `C:\WorkSpace\bli-youtube` and `D:\bili_yt_dl` during `package_archive.py`, filtering non-whitelisted creators and rebuilding `search_fts`.
- `search_fts` uses SQLite FTS5 `tokenize = 'trigram'`: queries shorter than 3 characters return 0 hits.
- `BV19h411Y7bv_p1` and `BV19h411Y7bv_p2` share one ASR output path due to upstream filename truncation (1,980 DB rows -> 1,979 file sets in `bili_23191782`); preserve this mismatch faithfully rather than dropping a row.
- `restore.py` uses HTTP Range resume (`DOWNLOAD_RETRIES=6`) and extracts with `tarfile.extractall(..., filter="data")` on Python 3.12+ (plus explicit link/traversal checks on older Python).
