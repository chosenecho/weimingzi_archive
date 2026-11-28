# weimingzi_archive

创作者 **未明子**（B 站 [`UID 23191782`](https://space.bilibili.com/23191782) / YouTube [`@wujixuanshi`](https://www.youtube.com/@wujixuanshi)）公开内容与语音转录的结构化归档。仓库按内容日期快照持续维护，主分支只保留轻量目录、校验账本和脚本，大体积归档包托管在 [GitHub Releases](https://github.com/chosenecho/weimingzi_archive/releases)。

- **为什么用这份归档**：省去登录态、风控抓取与 1,690 小时视频的 GPU 转录成本，直接获取已脱敏的文本、21.7 万条精选评论和本地 SQLite FTS5 全文检索库；所有分包附 `SHA256SUMS.txt` 并支持断点续传。
- **不包含的内容**：原始 `.mp4` 视频实体（超 460 GB）与中间音频文件不托管于 GitHub。
- **许可边界**：脚本与文档采用 [MIT License](LICENSE)；归档语料与媒体权利仍归原权利人，使用与撤下说明见 [`CONTENT-RIGHTS.md`](CONTENT-RIGHTS.md)。

## 快速还原

需要 **Python 3.9+**，仅依赖标准库（无需安装第三方包，也无需 `gh` CLI）。

```bash
git clone https://github.com/chosenecho/weimingzi_archive.git
cd weimingzi_archive

# 1. 查看最新全量快照 + 增量补充包与体积（不下载）
python scripts/restore.py --preset full --list

# 2. 推荐：一键还原双流全量 + 最新增量轻量包（约 345.40 MiB，自动合并 SQLite 增量库）
#    输出到 data/：全部 ASR .txt/.srt + 可读 Markdown + 脱敏 SQLite 库
python scripts/restore.py --preset light
```

还原完成后可直接用 SQLite 查询本地全文索引（`search_fts` 使用 `trigram` 分词，检索词需 ≥ 3 字）：

```bash
python -c "import sqlite3; c=sqlite3.connect('data/db/biliyt.db'); print(c.execute(\"SELECT kind, published_at, display_title FROM search_fts WHERE search_fts MATCH '黑格尔' LIMIT 5\").fetchall())"
```

按需下载示例：

```bash
# 只下 ASR 语音转录（全量 + 增量：light 约 66.16 MiB；full 含词级 JSON 约 472.90 MiB）
python scripts/restore.py --modules asr --preset light

# 只下多平台归档图文（light + 4,527 张原图与封面，约 959.14 MiB）
python scripts/restore.py --modules archive --preset media

# 已下载过基线全量包时，仅拉取指定时间段增量包并自动合入本地 data/
python scripts/restore.py --tag asr-delta-2026.08.02-2026.11.26 --preset full
python scripts/restore.py --tag archive-delta-2026.11.24-2026.11.27 --preset full

# 指定版本与平台分组，或离线从本地目录校验还原
python scripts/restore.py --tag archive-v2026.11.27 --preset media --groups yt_wujixuanshi
python scripts/restore.py --src-dir ./downloaded_release --preset light
```

## 当前快照与增量包

两条上游流水线各自维护**全量基线快照**（`*-vYYYY.MM.DD`）与**时间段增量补充包**（`*-delta-YYYY.MM.DD-YYYY.MM.DD`），默认 `restore.py` 会按序还原基线并自动叠加增量与合并 SQLite：

| 发布流 | 类型与版本 | 时间跨度 | 覆盖内容 | 在线目录与账本 |
| --- | --- | --- | --- | --- |
| **ASR 转录** (`asr`) | 基线 [`asr-v2026.07.17`](https://github.com/chosenecho/weimingzi_archive/releases/tag/asr-v2026.07.17) | 2017-12-14 ~ 2026-07-17 | B 站 1,980 条 / 1,530.03h + YouTube 105 条 / 154.29h（共 2,084 组 `.txt`/`.srt`、1,979 份词级 `.json`、`asr_pipeline.db`） | [`asr_bili`](catalog/asr_bili_23191782.csv) · [`asr_yt`](catalog/asr_yt_wujixuanshi.csv) · [`MANIFEST`](manifests/asr-v2026.07.17/MANIFEST.json) |
| **ASR 转录** (`asr`) | 增量 [`asr-delta-2026.08.02-2026.11.26`](https://github.com/chosenecho/weimingzi_archive/releases/tag/asr-delta-2026.08.02-2026.11.26) | 2026-08-02 ~ 2026-11-26 | B 站新增 3 条有声视频 / 5.86h（`BV1fD3o69EiP`、`BV1S8hA6MEvy`、`BV1SmyCzNEqd`，含 `.txt`/`.srt`/词级 `.json` 与增量库，压缩 1.64 MiB） | [`MANIFEST`](manifests/asr-delta-2026.08.02-2026.11.26/MANIFEST.json) |
| **多平台归档** (`archive`) | 基线 [`archive-v2026.11.27`](https://github.com/chosenecho/weimingzi_archive/releases/tag/archive-v2026.11.27) | 2017-12-14 ~ 2026-11-27 | 视频元数据 1,797（B 站 1,691 + YT 106）、动态 2,773、专栏 71、精选评论 216,882、平台自带 `.srt` 1,905、图片/封面 4,525、`biliyt.db`（222,967 行 FTS5） | [`videos_bili`](catalog/videos_bili_23191782.csv) · [`videos_yt`](catalog/videos_yt_wujixuanshi.csv) · [`dynamics`](catalog/dynamics_bili_23191782.csv) · [`articles`](catalog/articles_bili_23191782.csv) · [`MANIFEST`](manifests/archive-v2026.11.27/MANIFEST.json) |
| **多平台归档** (`archive`) | 增量 [`archive-delta-2026.11.24-2026.11.27`](https://github.com/chosenecho/weimingzi_archive/releases/tag/archive-delta-2026.11.24-2026.11.27) | 2026-11-24 ~ 2026-11-27 | B 站新增 2 条 11-26 视频（`BV1SmyCzNEqd`、`BV11AyUzrEjr`）元信息/封面/89 条视频精选评论/1 份平台 `.srt` + 3 条直播动态 + 95 行 FTS5（压缩 712.05 KiB） | [`MANIFEST`](manifests/archive-delta-2026.11.24-2026.11.27/MANIFEST.json) |

> 累计覆盖（基线 + 增量）：ASR 共 **2,088 条 / 1,690.18h**（`2017-12-14 ~ 2026-11-26`）；多平台归档共 **1,799 条视频、2,776 条动态、71 篇专栏、216,971 条精选评论**。`bili_23191782` 中 `BV19h411Y7bv_p1` 与 `p2` 因上游文件名截断共用同一份 ASR 输出文件（1,983 条数据库记录对应 1,982 组文件）；完整统计见 [`catalog/stats.json`](catalog/stats.json)。

## 下载分级（`--preset`）

单个分包体积与 SHA-256 见对应版本的 `MANIFEST.json`，或运行 `python scripts/restore.py --preset full --list`。

| `--preset` | `asr` 包含内容（压缩体积） | `archive` 包含内容（压缩体积） |
| --- | --- | --- |
| `light`（默认） | `.txt` + `.srt` + `asr_pipeline.db`（65.93 MiB） | 可读 Markdown + `biliyt.db`（278.81 MiB） |
| `media` | 同 `light`（65.93 MiB） | `light` + 原图与封面（958.67 MiB） |
| `raw` | `light` + 词级时间戳 `.json`（471.26 MiB） | `light` + 原始元信息/评论 `.json` + 平台 `.srt`（509.34 MiB） |
| `full` | 全部 ASR 分包（471.26 MiB） | 全部归档分包（1.16 GiB） |
| `db` | 仅 `asr_pipeline.db`（491.67 KiB） | 仅 `biliyt.db`（258.18 MiB） |

## 相关文档

- [`docs/LAYOUT.md`](docs/LAYOUT.md)：仓库与 `data/` 目录树、分包命名约定、SQLite 表结构
- [`docs/MAINTENANCE.md`](docs/MAINTENANCE.md)：双流水线打包、发版与验收流程（上游工具：[qwen3-asr-pipeline](https://github.com/chosenecho/qwen3-asr-pipeline)、[biliyt-archiver](https://github.com/chosenecho/biliyt-archiver)）
- [`CONTENT-RIGHTS.md`](CONTENT-RIGHTS.md)：内容权利归属、使用边界与争议处理
- [`LICENSE`](LICENSE)：仓库代码与文档的 MIT 许可
