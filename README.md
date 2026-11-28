# 未明子全平台公开内容归档 (`weimingzi_archive`)

面向创作者 **未明子（B 站 UID `23191782` / YouTube `@wujixuanshi`）** 公开视频语音转录与多平台内容的长期结构化归档仓库。

- **Git 主分支**仅保存轻量、可在线检索的文本目录（[`catalog/`](catalog/)）、版本哈希账本（[`manifests/`](manifests/)）、结构文档与纯标准库还原脚本；
- **大体积归档分包与 SQLite 数据库**托管于 [GitHub Releases](https://github.com/chosenecho/weimingzi_archive/releases)，按**双流水线独立发布**（`asr-vYYYY.MM.DD` 与 `archive-vYYYY.MM.DD`，版本号均取自对应流水线最新内容日期），支持自动校验 SHA-256、HTTP 断点续传与一键还原；
- **原始 `.mp4` 视频实体**（超 460 GB）与中间音频文件不托管于 GitHub。

> 内容权利与撤下说明请参阅 [`CONTENT-RIGHTS.md`](CONTENT-RIGHTS.md)，代码与语料许可界定请参阅 [`LICENSE`](LICENSE)。

---

## 当前双流水线快照

| 发布流 | 最新标签 | 上游工具 | 版本日期依据 | 核心内容 | 清单账本 |
| --- | --- | --- | --- | --- | --- |
| **ASR 语音转录** (`asr`) | [`asr-v2026.07.17`](https://github.com/chosenecho/weimingzi_archive/releases/tag/asr-v2026.07.17) | [qwen3-asr-pipeline](https://github.com/chosenecho/qwen3-asr-pipeline) | 最新 ASR `.txt` 对应视频日期 | 2,084 组纯文本/字幕、1,979 份词级 JSON、脱敏 `asr_pipeline.db` | [`manifests/asr-v2026.07.17/`](manifests/asr-v2026.07.17/) |
| **多平台内容归档** (`archive`) | [`archive-v2026.11.27`](https://github.com/chosenecho/weimingzi_archive/releases/tag/archive-v2026.11.27) | [biliyt-archiver](https://github.com/chosenecho/biliyt-archiver) | 最新 B 站动态发布日期 | 1,797 条视频元数据、2,773 条动态、71 篇专栏、216,882 条精选评论、1,905 份平台自带字幕、4,525 张原图与封面、脱敏 FTS5 `biliyt.db` | [`manifests/archive-v2026.11.27/`](manifests/archive-v2026.11.27/) |

### 1. ASR 语音转录覆盖（`asr-v2026.07.17`）

| 来源分组 | 平台主页 | 数据库记录 | 转录文件（`.txt` / `.srt` / `.json`） | 累计时长 | 时间跨度 | 在线目录 |
| --- | --- | ---: | --- | ---: | --- | --- |
| `bili_23191782` | [B 站 UID 23191782](https://space.bilibili.com/23191782) | 1,980 | 1,979 / 1,979 / 1,979 | 1,530.03 小时 | 2017-12-14 ~ 2026-07-17 | [`catalog/asr_bili_23191782.csv`](catalog/asr_bili_23191782.csv) |
| `yt_wujixuanshi` | [YouTube @wujixuanshi](https://www.youtube.com/@wujixuanshi) | 105 | 105 / 105 / 0 | 154.29 小时 | 2020-12-23 ~ 2025-09-27 | [`catalog/asr_yt_wujixuanshi.csv`](catalog/asr_yt_wujixuanshi.csv) |
| **合计** | — | **2,085** | **2,084 / 2,084 / 1,979** | **1,684.32 小时** | **2017-12-14 ~ 2026-07-17** | [`catalog/stats.json`](catalog/stats.json) |

> 注：`bili_23191782` 中 `BV19h411Y7bv_p1` 与 `BV19h411Y7bv_p2` 共用同一份输出文件（`2021-05-24 - 【主义主义】致分哲小将2.*`），因此 1,980 条记录对应 1,979 组磁盘文件。

### 2. 多平台内容归档覆盖（`archive-v2026.11.27`）

| 来源分组 | 视频元数据 | 平台自带 `.srt` | 动态 | 专栏 | 精选评论 | 图片/封面 | 在线目录 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `bili_23191782` | 1,691（2017-12-14 ~ 2026-08-27） | 1,903 | 2,773（2017-12-14 ~ 2026-11-27） | 71（2021-02-08 ~ 2026-06-18） | 216,882 | 4,419 | [`videos`](catalog/videos_bili_23191782.csv) · [`dynamics`](catalog/dynamics_bili_23191782.csv) · [`articles`](catalog/articles_bili_23191782.csv) |
| `yt_wujixuanshi` | 106（2020-12-23 ~ 2026-07-14） | 2 | 0 | 0 | 0 | 106 | [`videos`](catalog/videos_yt_wujixuanshi.csv) |
| **合计** | **1,797** | **1,905** | **2,773** | **71** | **216,882** | **4,525** | [`catalog/stats.json`](catalog/stats.json) |

> 注：精选评论由视频评论区（89,779 条 / 1,690 个视频）、动态评论区（125,033 条 / 2,766 条动态）与专栏评论区（2,070 条 / 71 篇专栏）汇总而成，含 519 条置顶评论与 2,391 条 UP 主本人回复；合并脱敏数据库 `data/db/biliyt.db` 内置 `search_fts`（222,967 条 FTS5 全文索引）。

---

## 快速还原到本地

还原脚本 [`scripts/restore.py`](scripts/restore.py) 仅使用 Python 3 标准库，**无需安装第三方依赖，也无需 `gh` CLI**，会自动解析各模块最新 Release 标签、下载分包（支持断点续传）、校验 `SHA256SUMS.txt` 并安全解压至 `data/` 目录。

```bash
git clone https://github.com/chosenecho/weimingzi_archive.git
cd weimingzi_archive

# 1. 推荐：一键还原双流水线轻量包（约 344.74 MiB）
#    含全部 ASR .txt/.srt + asr_pipeline.db + 动态/专栏/视频可读 Markdown + FTS5 biliyt.db
python scripts/restore.py --preset light

# 2. 仅还原 ASR 语音转录流（light 约 65.93 MiB；full 含词级 JSON 约 471.26 MiB）
python scripts/restore.py --modules asr --preset light
python scripts/restore.py --modules asr --preset full

# 3. 仅还原多平台内容归档流（按需分层）
python scripts/restore.py --modules archive --preset light   # 约 278.81 MiB：可读 Markdown + FTS5 biliyt.db
python scripts/restore.py --modules archive --preset media   # 约 958.67 MiB：light + 4,525 张原图与封面
python scripts/restore.py --modules archive --preset raw     # 约 509.34 MiB：light + 原始 JSON + 平台自带 .srt
python scripts/restore.py --modules archive --preset full    # 约 1.16 GiB：全量归档分包

# 4. 仅下载脱敏 SQLite 数据库（asr_pipeline.db + biliyt.db，约 258.66 MiB）
python scripts/restore.py --preset db
```

常用可选参数：

```bash
# 仅列出双流水线最新快照的分包清单与体积（不下载）
python scripts/restore.py --preset full --list

# 指定单一 Release 标签与单一平台分组
python scripts/restore.py --tag archive-v2026.11.27 --preset media --groups yt_wujixuanshi

# 离线模式：从已手动下载好某个 Release 文件的本地目录校验并还原
python scripts/restore.py --src-dir ./downloaded_release --preset light
```

---

## Release 资产分级

### ASR 转录流（`asr-v2026.07.17`）

完整哈希见 [`manifests/asr-v2026.07.17/MANIFEST.json`](manifests/asr-v2026.07.17/MANIFEST.json) 与 [`SHA256SUMS.txt`](manifests/asr-v2026.07.17/SHA256SUMS.txt)。

| 分级 (`--preset`) | 资产文件 | 内容 | 文件数 | 解压后 | 压缩包 |
| --- | --- | --- | ---: | ---: | ---: |
| `light` | `asr-bili_23191782-text.tar.gz` | B 站 `.txt` + `.srt` | 3,958 | 174.87 MiB | 58.97 MiB |
| `light` | `asr-yt_wujixuanshi-text.tar.gz` | YouTube `.txt` + `.srt` | 210 | 18.22 MiB | 6.47 MiB |
| `light` / `db` | `asr-pipeline-db.tar.gz` | 脱敏 `data/db/asr_pipeline.db` | 1 | 3.04 MiB | 491.67 KiB |
| `full` | `asr-bili_23191782-json-part01..05.tar.gz` | B 站词级时间戳 `.json`（5 个独立分包） | 1,979 | 2.09 GiB | 405.33 MiB |

### 多平台内容归档流（`archive-v2026.11.27`）

完整哈希见 [`manifests/archive-v2026.11.27/MANIFEST.json`](manifests/archive-v2026.11.27/MANIFEST.json) 与 [`SHA256SUMS.txt`](manifests/archive-v2026.11.27/SHA256SUMS.txt)。

| 分级 (`--preset`) | 资产文件 | 内容 | 文件数 | 解压后 | 压缩包 |
| --- | --- | --- | ---: | ---: | ---: |
| `light` | `archive-bili_23191782-md.tar.gz` | B 站 UP 主档案、动态、专栏、视频与评论可读 Markdown | 7,377 | 48.86 MiB | 20.63 MiB |
| `light` / `db` | `archive-biliyt-db.tar.gz` | 合并脱敏 `data/db/biliyt.db`（含 FTS5 全文检索表） | 1 | 621.08 MiB | 258.18 MiB |
| `media` / `full` | `archive-bili_23191782-images-part01..02.tar.gz` | B 站动态/专栏配图、头像横幅与视频封面（2 分包） | 4,419 | 712.12 MiB | 676.38 MiB |
| `media` / `full` | `archive-yt_wujixuanshi-images.tar.gz` | YouTube 视频封面 | 106 | 4.03 MiB | 3.48 MiB |
| `raw` / `full` | `archive-bili_23191782-meta-json.tar.gz` | B 站动态/专栏/视频原始元数据 `.json` | 4,911 | 199.06 MiB | 50.66 MiB |
| `raw` / `full` | `archive-bili_23191782-platform-srt.tar.gz` | B 站平台自带字幕 `.srt` | 1,903 | 117.69 MiB | 40.44 MiB |
| `raw` / `full` | `archive-bili_23191782-comments-json-part01..04.tar.gz` | B 站动态/专栏/视频原始评论 `.json`（4 分包） | 4,529 | 1.74 GiB | 138.71 MiB |
| `raw` / `full` | `archive-yt_wujixuanshi-raw.tar.gz` | YouTube 原始元数据 `.json` + 平台自带 `.srt` | 108 | 6.34 MiB | 731.52 KiB |

---

## 仓库结构与维护文档

详细目录规范与命名约定见 [`docs/LAYOUT.md`](docs/LAYOUT.md)，双流水线独立打包与发版 SOP 见 [`docs/MAINTENANCE.md`](docs/MAINTENANCE.md)。

### 模块进度

- [x] **ASR 语音转录归档（`asr`）**：基于 [chosenecho/qwen3-asr-pipeline](https://github.com/chosenecho/qwen3-asr-pipeline) 生成，独立发布流 `asr-vYYYY.MM.DD`（当前 `asr-v2026.07.17`）。
- [x] **多平台内容归档（`archive`）**：基于 [chosenecho/biliyt-archiver](https://github.com/chosenecho/biliyt-archiver) 生成，涵盖 B 站动态、专栏、视频元数据、精选评论、平台自带字幕、原图与封面及 FTS5 `biliyt.db`，独立发布流 `archive-vYYYY.MM.DD`（当前 `archive-v2026.11.27`）。

---

## 许可与权利声明

- 仓库中的脚本与文档按 **MIT License** 开源（详见 [`LICENSE`](LICENSE)）。
- 归档的转录文本、动态、专栏、评论、图片、字幕及相关元数据来源于创作者公开内容，权利仍归原权利人所有，本项目不对其授予统一开源许可，仅用于个人存档、学习检索与研究用途；权利人移除联系渠道见 [`CONTENT-RIGHTS.md`](CONTENT-RIGHTS.md)。
