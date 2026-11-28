# 维护与发版 SOP

本文档面向仓库维护者，说明如何独立打包并发布两条上游流水线的日期版本快照：

- **ASR 语音转录流（`asr-vYYYY.MM.DD`）**：上游 [chosenecho/qwen3-asr-pipeline](https://github.com/chosenecho/qwen3-asr-pipeline)，版本号取自最新 ASR `.txt` 对应视频日期；
- **多平台内容归档流（`archive-vYYYY.MM.DD`）**：上游 [chosenecho/biliyt-archiver](https://github.com/chosenecho/biliyt-archiver)，版本号取自最新 B 站动态发布日期。

两套脚本均支持 `--tag auto` 自动推导内容日期标签，并共用严格创作者白名单（仅 `bili_23191782` 与 `yt_wujixuanshi`）。

---

## 1. 发布新的 ASR 日期快照（`asr-v*`）

### 第一步：本地打包与目录更新

```bash
python scripts/package_asr.py package \
  --tag auto \
  --asr-root "C:\WorkSpace\asr" \
  --update-repo-metadata
```

该命令会自动完成：
1. 校验创作者白名单并扫描转录文件，自动计算最新内容日期标签（亦可手工指定 `--tag asr-vYYYY.MM.DD`）；
2. 导出 WAL 合并、移除服务端 URL 与本机绝对路径的脱敏 `asr_pipeline.db`；
3. 按层生成 UTF-8 PAX `.tar.gz` 分包到 `release_dist/<tag>/`；
4. 生成 `MANIFEST.json` 与 `SHA256SUMS.txt`，并同步更新仓库内的 `catalog/asr_*.csv`、`catalog/stats.json` 与 `manifests/<tag>/`。

### 第二步：提交元数据、发布 Release 并清理暂存目录

```bash
git status -sb
git add catalog/ manifests/ README.md
git commit -m "chore(release): record ASR snapshot <tag>"
git push

python scripts/package_asr.py publish \
  --tag <tag> \
  --skip-package \
  --clean
```

---

## 2. 发布新的多平台归档快照（`archive-v*`）

### 第一步：本地打包与目录更新

```bash
python scripts/package_archive.py package \
  --tag auto \
  --biliyt-root "C:\WorkSpace\bli-youtube" \
  --videos-root "D:\bili_yt_dl" \
  --update-repo-metadata
```

该命令会自动完成：
1. 仅扫描白名单创作者（`bili_23191782`、`yt_wujixuanshi`），严格排除 `.mp4` / `.mkv` / `.m4a` 等音视频二进制文件；
2. 将 B 站归档库（含历史视频元信息与评论）与 YouTube 视频元信息合并入脱敏 `data/db/biliyt.db`（过滤非白名单创作者、清空 `crawl_runs`、归一化所有相对路径并重建 `search_fts` FTS5 索引）；
3. 按 `light`（Markdown + DB）、`images`（原图与封面，超 450 MB 自动拆包）、`raw`（原始元数据 JSON、评论 JSON 拆包、平台自带 `.srt`）三层打包至 `release_dist/<tag>/`；
4. 更新仓库内 `catalog/videos_*.csv`、`catalog/dynamics_bili_23191782.csv`、`catalog/articles_bili_23191782.csv`、`catalog/stats.json` 与 `manifests/<tag>/`。

### 第二步：提交元数据、发布 Release 并清理暂存目录

```bash
git status -sb
git add catalog/ manifests/ README.md
git commit -m "chore(release): record archive snapshot <tag>"
git push

python scripts/package_archive.py publish \
  --tag <tag> \
  --skip-package \
  --clean
```

---

## 3. 端到端验收

发布后在临时目录以普通用户视角验证自动解析最新双流标签、清单查看、哈希校验与安全解压：

```bash
# 仅检查最新双流分包清单
python scripts/restore.py --preset full --list

# 实测下载、SHA-256 校验与解压（完成后删除 ./_verify_tmp）
python scripts/restore.py --modules archive --preset light --groups yt_wujixuanshi --dest ./_verify_tmp
python scripts/restore.py --modules asr --preset light --groups yt_wujixuanshi --dest ./_verify_tmp
```

---

## 4. 安全检查红线

- 严禁将 `cookies/`、`.env`、`config.yaml`、`crawl_runs` 运行记录或任何其他创作者（非 `bili_23191782` / `yt_wujixuanshi`）的数据带入本仓库或 Release 资产。
- 严禁打包原始 `.mp4` 等音视频文件。
- 每次发版前确认 `asr_pipeline.db`、`biliyt.db` 与 `catalog/*.csv` 中不含本机盘符绝对路径（如 `C:\`、`D:\`）、用户名、Cookie 字段或私密服务端 URL。
