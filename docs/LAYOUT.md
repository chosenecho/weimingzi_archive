# 目录结构与发布规范

## 1. Git 仓库布局（轻量常驻）

Git 主分支只保存可审计、可在线浏览的文本索引、清单与工具脚本，不直接提交大体积压缩包、数据库或媒体文件：

```text
weimingzi_archive/
├── README.md                     # 项目说明与快速还原指引
├── CONTENT-RIGHTS.md             # 内容权利说明
├── LICENSE                       # 许可声明（代码 MIT，归档语料不授予统一许可）
├── catalog/                      # 轻量化全量目录（可在 GitHub 网页直接浏览/搜索）
│   ├── stats.json                # 双流水线最新快照覆盖统计
│   ├── asr_bili_23191782.csv     # B 站视频 ASR 转录目录
│   ├── asr_yt_wujixuanshi.csv    # YouTube 视频 ASR 转录目录
│   ├── videos_bili_23191782.csv  # B 站视频元数据与字幕/评论目录
│   ├── videos_yt_wujixuanshi.csv # YouTube 视频元数据目录
│   ├── dynamics_bili_23191782.csv# B 站动态目录
│   └── articles_bili_23191782.csv# B 站专栏目录
├── manifests/                    # 各流水线快照版本的 MANIFEST.json 与 SHA256SUMS.txt 账本
│   ├── asr-vYYYY.MM.DD/
│   └── archive-vYYYY.MM.DD/
├── docs/                         # 结构规范与维护文档
└── scripts/                      # 打包、发布与还原脚本（纯 Python 标准库）
    ├── package_asr.py            # ASR 转录流水线打包与发布脚本
    ├── package_archive.py        # 多平台内容归档流水线打包与发布脚本
    └── restore.py                # 双流水线一键下载、校验与还原脚本
```

## 2. 还原后的本地目录树（`data/`）

从 GitHub Releases 下载并运行 `python scripts/restore.py` 后，所有资产按统一相对路径解压到仓库根目录的 `data/` 下（已被 `.gitignore` 忽略）：

```text
data/
├── db/
│   ├── asr_pipeline.db           # ASR 流水线脱敏状态库（已将路径归一化为相对路径）
│   └── biliyt.db                 # 多平台归档合并脱敏库（含 profiles/videos/dynamics/articles/comments/search_fts）
├── transcripts/                  # Qwen3-ASR 语音转录产物（来自 asr-v*）
│   ├── bili_23191782/            # *.txt / *.srt /（可选词级）*.json
│   └── yt_wujixuanshi/           # *.txt / *.srt
└── archive/                      # biliyt-archiver 多平台归档产物（来自 archive-v*）
    ├── bilibili/bili_23191782/   # profile、dynamics/、articles/、videos/、comments/、images/
    └── youtube/yt_wujixuanshi/   # videos/（.info.json / 平台自带 .srt / 封面图片）
```

## 3. 双流水线版本号与资产命名约定

- **独立发布流标签**：两条上游流水线更新频率不同，彼此独立发版，版本号取自该流水线**最新内容日期**（同一天如需修订追加 `-r2`）：
  - ASR 流水线：`asr-vYYYY.MM.DD`（取最新 ASR `.txt` 对应视频日期，如 `asr-v2026.07.17`）
  - 多平台归档流水线：`archive-vYYYY.MM.DD`（取最新 B 站动态日期，如 `archive-v2026.11.27`）
- **资产命名与分层**：
  - ASR 流（`asr-*`）：
    - `asr-<group>-text.tar.gz`（`tier=light`）：纯文本 `.txt` + 字幕 `.srt`
    - `asr-<group>-json-partXX.tar.gz`（`tier=full`）：词级时间戳 `.json`（按原始体积 ≤ 450 MB 拆包）
    - `asr-pipeline-db.tar.gz`（`tier=light`, `kind=sqlite-db`）：脱敏 `data/db/asr_pipeline.db`
  - 多平台归档流（`archive-*`）：
    - `archive-bili_23191782-md.tar.gz`（`tier=light`）：UP 主档案、动态、专栏、视频与评论可读 Markdown
    - `archive-biliyt-db.tar.gz`（`tier=light`, `kind=sqlite-db`）：合并脱敏 `data/db/biliyt.db`（含 FTS5 索引）
    - `archive-<group>-images[-partXX].tar.gz`（`tier=images`）：动态/专栏配图、头像横幅与视频封面
    - `archive-<group>-meta-json.tar.gz` / `platform-srt.tar.gz` / `comments-json-partXX.tar.gz` / `raw.tar.gz`（`tier=raw`）：原始接口元数据 `.json`、原始评论 `.json` 与平台自带 `.srt`
- **归档格式**：所有压缩包均为 UTF-8 PAX 格式 `.tar.gz`，随附 `MANIFEST.json` 与 `SHA256SUMS.txt`；单个资产体积严格低于 GitHub Releases 2 GiB 上限。
- **排除项**：原始 `.mp4` 视频实体（总计超过 460 GB）、中间音频文件、本地运行日志表（`crawl_runs`）及任何非白名单创作者数据均不包含在仓库或 Release 中。
