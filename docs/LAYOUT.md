# 目录与数据结构

## 1. Git 仓库目录

主分支只提交轻量文本索引、哈希账本、脚本和文档：

```text
weimingzi_archive/
├── README.md                     # 入口说明与快速还原
├── AGENTS.md                     # AI Agent 协作约定
├── CONTENT-RIGHTS.md             # 权利归属与争议处理
├── LICENSE                       # 代码与文档 MIT 许可
├── catalog/                      # 在线可检索目录（UTF-8 CSV + stats.json）
│   ├── stats.json
│   ├── asr_bili_23191782.csv
│   ├── asr_yt_wujixuanshi.csv
│   ├── videos_bili_23191782.csv
│   ├── videos_yt_wujixuanshi.csv
│   ├── dynamics_bili_23191782.csv
│   └── articles_bili_23191782.csv
├── manifests/                    # 各快照版本的 MANIFEST.json 与 SHA256SUMS.txt
│   ├── asr-vYYYY.MM.DD/
│   └── archive-vYYYY.MM.DD/
├── docs/
│   ├── LAYOUT.md                 # 本文件
│   └── MAINTENANCE.md            # 发版与验收流程
└── scripts/
    ├── package_asr.py            # ASR 流打包与发布
    ├── package_archive.py        # 多平台归档流打包与发布
    └── restore.py                # 下载、校验与还原
```

## 2. 还原后的 `data/` 目录

运行 `python scripts/restore.py` 后，资产解压至仓库根目录下的 `data/`（已被 `.gitignore` 忽略）：

```text
data/
├── db/
│   ├── asr_pipeline.db           # ASR 状态库（相对路径，已脱敏）
│   ├── biliyt.db                 # 多平台合并库（相对路径，已脱敏，含 FTS5）
│   └── deltas/                   # 时间段增量库（restore.py 自动合入上述基线库）
├── transcripts/                  # 来自 asr-v* 与 asr-delta-*
│   ├── bili_23191782/            # *.txt / *.srt / *.json（词级时间戳）
│   └── yt_wujixuanshi/           # *.txt / *.srt
└── archive/                      # 来自 archive-v* 与 archive-delta-*
    ├── bilibili/bili_23191782/   # profile、dynamics/、articles/、videos/、comments/、images/
    └── youtube/yt_wujixuanshi/   # videos/（.info.json、平台 .srt、封面）
```

## 3. 版本标签与分包约定

- **独立版本流与增量流**：按各自最新内容日期打标，同日修订追加 `-r2`。
  - 全量基线：`asr-vYYYY.MM.DD`（取最新 ASR `.txt` 对应的视频日期）、`archive-vYYYY.MM.DD`（取最新 B 站动态日期）
  - 时间段增量：`asr-delta-YYYY.MM.DD-YYYY.MM.DD`、`archive-delta-YYYY.MM.DD-YYYY.MM.DD`（仅打包区间新增文件与 `data/db/deltas/*.db`，不覆写基线库，由 `restore.py` 自动合并）
- **归档格式**：UTF-8 PAX `.tar.gz`，大体积分组按原始体积 ≤ 450 MB 拆分为 `-partXX.tar.gz`，单包严格低于 GitHub Releases 2 GiB 限制，随附 `MANIFEST.json` 与 `SHA256SUMS.txt`。
- **分包前缀**：
  - `asr-<group>-text.tar.gz`（`light`）、`asr-<group>-json-partXX.tar.gz`（`full`）、`asr-pipeline-db.tar.gz`（`light` / `sqlite-db`）
  - `archive-<group>-md.tar.gz`（`light`）、`archive-biliyt-db.tar.gz`（`light` / `sqlite-db`）、`archive-<group>-images[-partXX].tar.gz`（`images`）、`archive-<group>-{meta-json,platform-srt,comments-json-partXX,raw}.tar.gz`（`raw`）
- **排除项**：`.mp4`/`.mkv`/`.m4a` 等音视频文件、中间缓存、`sync_runs` 本地运行日志、Cookies/配置文件及任何非白名单创作者数据。

## 4. 数据库核心表

### `data/db/biliyt.db`

| 表名 | 说明 |
| --- | --- |
| `profiles` / `profile_snapshots` | UP 主资料与粉丝/播放快照（仅白名单 `target_id`） |
| `videos` | B 站与 YouTube 视频元数据（合并自历史下载目录与归档库） |
| `dynamics` / `articles` | B 站动态与专栏正文、统计与本地相对路径 |
| `comments` | 视频、动态、专栏精选评论（含 `is_pinned`、`is_up_comment`） |
| `search_fts` | FTS5 虚拟表（`tokenize = 'trigram'`，检索词需 ≥ 3 字），索引视频、动态、专栏与评论 |
| `sync_runs` | 已清空（不导出本机运行记录） |

### `data/db/asr_pipeline.db`

| 表名 | 说明 |
| --- | --- |
| `media_items` | 转录任务记录，含平台分组、视频 ID、时长、状态与归一化后的输出相对路径 |
| `pipeline_runs` | 脱敏后的流水线运行统计（已移除服务端 URL 与本机路径） |
