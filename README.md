# 未明子全平台公开内容归档 (`weimingzi_archive`)

面向创作者 **未明子（B 站 UID `23191782` / YouTube `@wujixuanshi`）** 公开视频语音转录与多平台内容的长期结构化归档仓库。

- **Git 主分支**仅保存轻量、可在线检索的文本目录（[`catalog/`](catalog/)）、版本哈希账本（[`manifests/`](manifests/)）、结构文档与纯标准库还原脚本；
- **大体积转录包与 SQLite 数据库**托管于 [GitHub Releases](https://github.com/chosenecho/weimingzi_archive/releases)，按日期快照（CalVer `vYYYY.MM.DD`）分层发布，支持自动校验 SHA-256 与一键还原；
- **原始 `.mp4` 视频实体**（超 460 GB）与中间音频文件不托管于 GitHub。

> 内容权利与撤下说明请参阅 [`CONTENT-RIGHTS.md`](CONTENT-RIGHTS.md)，代码与语料许可界定请参阅 [`LICENSE`](LICENSE)。

---

## 当前快照：`v2026.11.27`

| 来源分组 | 平台主页 | 数据库记录 | 转录文件（`.txt` / `.srt` / `.json`） | 累计时长 | 时间跨度 | 在线目录 |
| --- | --- | ---: | --- | ---: | --- | --- |
| `bili_23191782` | [B 站 UID 23191782](https://space.bilibili.com/23191782) | 1,980 | 1,979 / 1,979 / 1,979 | 1,530.03 小时 | 2017-12-14 ~ 2026-07-17 | [`catalog/asr_bili_23191782.csv`](catalog/asr_bili_23191782.csv) |
| `yt_wujixuanshi` | [YouTube @wujixuanshi](https://www.youtube.com/@wujixuanshi) | 105 | 105 / 105 / 0 | 154.29 小时 | 2020-12-23 ~ 2025-09-27 | [`catalog/asr_yt_wujixuanshi.csv`](catalog/asr_yt_wujixuanshi.csv) |
| **合计** | — | **2,085** | **2,084 / 2,084 / 1,979** | **1,684.32 小时** | **2017-12-14 ~ 2026-07-17** | [`catalog/stats.json`](catalog/stats.json) |

> 注：`bili_23191782` 中 `BV19h411Y7bv_p1` 与 `BV19h411Y7bv_p2` 共用同一份输出文件（`2021-05-24 - 【主义主义】致分哲小将2.*`），因此 1,980 条记录对应 1,979 组磁盘文件；详情记录于 [`catalog/stats.json`](catalog/stats.json)。

---

## 快速还原到本地

还原脚本 [`scripts/restore.py`](scripts/restore.py) 仅使用 Python 3 标准库，**无需安装第三方依赖，也无需 `gh` CLI**，会自动从 GitHub Releases 拉取指定资产、验证 `SHA256SUMS.txt` 并安全解压至 `data/` 目录。

```bash
git clone https://github.com/chosenecho/weimingzi_archive.git
cd weimingzi_archive

# 1. 推荐：轻量核心包（约 65.93 MiB，含全部 .txt + .srt + 脱敏 SQLite 数据库）
python scripts/restore.py --preset light

# 2. 完整包（约 471.26 MiB，额外包含 B 站词级时间戳 .json 分包）
python scripts/restore.py --preset full

# 3. 仅下载脱敏数据库（约 491.67 KiB）
python scripts/restore.py --preset db
```

常用可选参数：

```bash
# 仅列出最新快照包含的分包及大小（不下载）
python scripts/restore.py --preset full --list

# 指定版本与单一分组
python scripts/restore.py --tag v2026.11.27 --preset light --groups yt_wujixuanshi

# 离线模式：从已手动下载好 Release 文件的本地目录校验并还原
python scripts/restore.py --src-dir ./downloaded_release --preset light
```

---

## Release 资产分级（`v2026.11.27`）

完整哈希与分包元数据见 [`manifests/v2026.11.27/MANIFEST.json`](manifests/v2026.11.27/MANIFEST.json) 与 [`manifests/v2026.11.27/SHA256SUMS.txt`](manifests/v2026.11.27/SHA256SUMS.txt)。

| 分级 (`--preset`) | 资产文件 | 内容 | 文件数 | 解压后 | 压缩包 |
| --- | --- | --- | ---: | ---: | ---: |
| `light` | `asr-bili_23191782-text.tar.gz` | B 站 `.txt` + `.srt` | 3,958 | 174.87 MiB | 58.97 MiB |
| `light` | `asr-yt_wujixuanshi-text.tar.gz` | YouTube `.txt` + `.srt` | 210 | 18.22 MiB | 6.47 MiB |
| `light` / `db` | `asr-pipeline-db.tar.gz` | 脱敏 `data/db/asr_pipeline.db` | 1 | 3.04 MiB | 491.67 KiB |
| `full` | `asr-bili_23191782-json-part01..05.tar.gz` | B 站词级时间戳 `.json`（5 个独立分包） | 1,979 | 2.09 GiB | 405.33 MiB |

---

## 仓库结构与后续规划

详细目录规范与命名约定见 [`docs/LAYOUT.md`](docs/LAYOUT.md)，维护与发版 SOP 见 [`docs/MAINTENANCE.md`](docs/MAINTENANCE.md)。

### 模块进度

- [x] **ASR 语音转录归档（`asr`）**：基于 [chosenecho/qwen3-asr-pipeline](https://github.com/chosenecho/qwen3-asr-pipeline) 生成，已完成首版快照 `v2026.11.27` 发布。
- [ ] **多平台文字与元数据归档（`archive`）**：计划接入 [chosenecho/biliyt-archiver](https://github.com/chosenecho/biliyt-archiver)，补充 B 站动态、专栏、视频元数据、精选评论、配图以及统一 SQLite FTS5 全文检索库（`data/db/weimingzi.db`）。

---

## 许可与权利声明

- 仓库中的脚本与文档按 **MIT License** 开源（详见 [`LICENSE`](LICENSE)）。
- 归档的转录文本、字幕及相关元数据来源于创作者公开内容，权利仍归原权利人所有，本项目不对其授予统一开源许可，仅用于个人存档、学习检索与研究用途；权利人移除联系渠道见 [`CONTENT-RIGHTS.md`](CONTENT-RIGHTS.md)。
