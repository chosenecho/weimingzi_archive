# 目录结构与发布规范

## 1. Git 仓库布局（轻量常驻）

Git 主分支只保存可审计、可在线浏览的文本索引、清单与工具脚本，不直接提交大体积压缩包、数据库或媒体文件：

```text
weimingzi_archive/
├── README.md                 # 项目说明与快速还原指引
├── CONTENT-RIGHTS.md         # 内容权利说明
├── LICENSE                   # 许可声明（代码 MIT，归档语料不授予统一许可）
├── catalog/                  # 轻量化全量目录（可在 GitHub 网页直接浏览/搜索）
│   ├── stats.json            # 最新快照覆盖统计
│   ├── asr_bili_23191782.csv # B 站视频 ASR 转录目录
│   └── asr_yt_wujixuanshi.csv# YouTube 视频 ASR 转录目录
├── manifests/                # 各日期版本的 MANIFEST.json 与 SHA256SUMS.txt 账本
│   └── vYYYY.MM.DD/
├── docs/                     # 结构规范与维护文档
└── scripts/                  # 打包、发布与还原脚本（纯 Python 标准库）
```

## 2. 还原后的本地目录树（`data/`）

从 GitHub Releases 下载并运行 `python scripts/restore.py` 后，所有资产按统一相对路径解压到仓库根目录的 `data/` 下（已被 `.gitignore` 忽略）：

```text
data/
├── db/
│   ├── asr_pipeline.db       # ASR 流水线脱敏状态库（已将路径归一化为相对路径）
│   └── weimingzi.db          # [规划中] 统一全形态检索库（biliyt + ASR FTS5）
├── transcripts/              # Qwen3-ASR 转录产物
│   ├── bili_23191782/        # *.txt / *.srt /（可选词级）*.json
│   └── yt_wujixuanshi/       # *.txt / *.srt
└── archive/                  # [规划中] biliyt-archiver 产物（动态、专栏、视频元数据、评论、图片）
    ├── bilibili/bili_23191782/
    └── youtube/yt_wujixuanshi/
```

## 3. 版本号与资产命名约定

- **版本标签**：统一采用带日期的 CalVer 标签 `vYYYY.MM.DD`（如 `v2026.11.27`）。同一天如需修订，追加 `-r2` 后缀。
- **资产命名**：按模块前缀区分，避免未来合并 `biliyt-archiver` 资产时冲突：
  - `asr-<group>-text.tar.gz`：纯文本 `.txt` + 字幕 `.srt`（核心轻量包）
  - `asr-<group>-json-partXX.tar.gz`：词级时间戳 `.json`（大分组按原始体积 ≤ 450 MB 拆包）
  - `asr-pipeline-db.tar.gz`：经过 WAL 合并与脱敏的 `data/db/asr_pipeline.db`
  - （后续接入）`archive-*`、`images-*`、`weimingzi-db.tar.gz`
- **归档格式**：所有压缩包均为 UTF-8 PAX 格式 `.tar.gz`，随附 `MANIFEST.json` 与 `SHA256SUMS.txt`。
- **排除项**：原始 `.mp4` 视频实体（总计超过 460 GB）与中间音频文件不托管于 GitHub。
