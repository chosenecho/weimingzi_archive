# weimingzi_archive

创作者 **未明子**（B 站 [`UID 23191782`](https://space.bilibili.com/23191782) / YouTube [`@wujixuanshi`](https://www.youtube.com/@wujixuanshi)）公开内容与语音转录的结构化归档。仓库按内容日期快照持续维护，主分支只保留轻量目录、校验账本和脚本，大体积归档包托管在 [GitHub Releases](https://github.com/chosenecho/weimingzi_archive/releases)。

- **为什么用这份归档**：省去登录态、风控抓取与 1,684 小时视频的 GPU 转录成本，直接获取已脱敏的文本、21.6 万条精选评论和本地 SQLite FTS5 全文检索库；所有分包附 `SHA256SUMS.txt` 并支持断点续传。
- **不包含的内容**：原始 `.mp4` 视频实体（超 460 GB）与中间音频文件不托管于 GitHub。
- **许可边界**：脚本与文档采用 [MIT License](LICENSE)；归档语料与媒体权利仍归原权利人，使用与撤下说明见 [`CONTENT-RIGHTS.md`](CONTENT-RIGHTS.md)。

## 快速还原

需要 **Python 3.9+**，仅依赖标准库（无需安装第三方包，也无需 `gh` CLI）。

```bash
git clone https://github.com/chosenecho/weimingzi_archive.git
cd weimingzi_archive

# 1. 查看最新双流快照包含的分包与体积（不下载）
python scripts/restore.py --preset full --list

# 2. 推荐：一键还原双流轻量包（约 344.74 MiB）
#    输出到 data/：全部 ASR .txt/.srt + 可读 Markdown + 脱敏 SQLite 库
python scripts/restore.py --preset light
```

还原完成后可直接用 SQLite 查询本地全文索引（`search_fts` 使用 `trigram` 分词，检索词需 ≥ 3 字）：

```bash
python -c "import sqlite3; c=sqlite3.connect('data/db/biliyt.db'); print(c.execute(\"SELECT kind, published_at, display_title FROM search_fts WHERE search_fts MATCH '黑格尔' LIMIT 5\").fetchall())"
```

按需下载示例：

```bash
# 只下 ASR 语音转录（light 约 65.93 MiB；full 含词级 JSON 约 471.26 MiB）
python scripts/restore.py --modules asr --preset light

# 只下多平台归档图文（light + 4,525 张原图与封面，约 958.67 MiB）
python scripts/restore.py --modules archive --preset media

# 指定版本与平台分组，或离线从本地目录校验还原
python scripts/restore.py --tag archive-v2026.11.27 --preset media --groups yt_wujixuanshi
python scripts/restore.py --src-dir ./downloaded_release --preset light
```

## 当前快照

两条上游流水线更新频率不同，各自按最新内容日期独立发版：

| 发布流 | 最新版本 | 时间跨度 | 覆盖内容 | 在线目录与账本 |
| --- | --- | --- | --- | --- |
| **ASR 转录** (`asr`) | [`asr-v2026.07.17`](https://github.com/chosenecho/weimingzi_archive/releases/tag/asr-v2026.07.17) | 2017-12-14 ~ 2026-07-17 | B 站 1,980 条 / 1,530.03h + YouTube 105 条 / 154.29h（共 2,084 组 `.txt`/`.srt`、1,979 份词级 `.json`、`asr_pipeline.db`） | [`asr_bili`](catalog/asr_bili_23191782.csv) · [`asr_yt`](catalog/asr_yt_wujixuanshi.csv) · [`MANIFEST`](manifests/asr-v2026.07.17/MANIFEST.json) |
| **多平台归档** (`archive`) | [`archive-v2026.11.27`](https://github.com/chosenecho/weimingzi_archive/releases/tag/archive-v2026.11.27) | 2017-12-14 ~ 2026-11-27 | 视频元数据 1,797（B 站 1,691 + YT 106）、动态 2,773、专栏 71、精选评论 216,882、平台自带 `.srt` 1,905、图片/封面 4,525、`biliyt.db`（222,967 行 FTS5） | [`videos_bili`](catalog/videos_bili_23191782.csv) · [`videos_yt`](catalog/videos_yt_wujixuanshi.csv) · [`dynamics`](catalog/dynamics_bili_23191782.csv) · [`articles`](catalog/articles_bili_23191782.csv) · [`MANIFEST`](manifests/archive-v2026.11.27/MANIFEST.json) |

> `bili_23191782` 中 `BV19h411Y7bv_p1` 与 `p2` 因上游文件名截断共用同一份输出文件，因此 ASR 1,980 条数据库记录对应 1,979 组文件；完整统计见 [`catalog/stats.json`](catalog/stats.json)。

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
