# 发版与维护流程

前提：本机具备 **Python 3.9+**、已登录的 `gh` CLI（`gh auth status`），以及两条上游流水线的最新本地产出。

- ASR 上游：[chosenecho/qwen3-asr-pipeline](https://github.com/chosenecho/qwen3-asr-pipeline)（默认目录 `C:\WorkSpace\asr`）
- 归档上游：[chosenecho/biliyt-archiver](https://github.com/chosenecho/biliyt-archiver)（默认目录 `C:\WorkSpace\bli-youtube` + `D:\bili_yt_dl`）

两个打包脚本都支持 `--tag auto` 自动推导内容日期标签，并共用白名单 `bili_23191782`、`yt_wujixuanshi`。完整参数见各脚本 `--help`。

## 1. 发布 ASR 快照（`asr-vYYYY.MM.DD`）

```bash
# 1) 打包到 release_dist/<tag>/ 并同步更新 catalog/ 与 manifests/<tag>/
python scripts/package_asr.py package --tag auto --update-repo-metadata

# 2) 检查变更、更新 README 快照数字后提交推送
git status -sb
git add catalog/ manifests/ README.md
git commit -m "chore(release): record ASR snapshot <tag>"
git push origin main

# 3) 上传 GitHub Release 并清理本地暂存包
python scripts/package_asr.py publish --tag <tag> --skip-package --clean
```

## 2. 发布多平台归档快照（`archive-vYYYY.MM.DD`）

```bash
# 1) 打包到 release_dist/<tag>/，合并脱敏 biliyt.db 并更新 catalog/ 与 manifests/<tag>/
python scripts/package_archive.py package --tag auto --update-repo-metadata

# 2) 检查变更、更新 README 快照数字后提交推送
git status -sb
git add catalog/ manifests/ README.md
git commit -m "chore(release): record archive snapshot <tag>"
git push origin main

# 3) 上传 GitHub Release 并清理本地暂存包
python scripts/package_archive.py publish --tag <tag> --skip-package --clean
```

## 3. 发版后验收

```bash
# 验证最新双流标签解析
python scripts/restore.py --preset full --list

# 实测下载、SHA-256 校验与解压（完成后删除 _verify_tmp）
python scripts/restore.py --modules asr --preset light --groups yt_wujixuanshi --dest ./_verify_tmp
python scripts/restore.py --modules archive --preset light --groups yt_wujixuanshi --dest ./_verify_tmp
```

## 4. 发版红线

- **白名单隔离**：只允许 `bili_23191782` 和 `yt_wujixuanshi` 进入仓库和 Release。
- **排除大媒体与敏感信息**：不打包 `.mp4`/`.mkv`/`.m4a` 等音视频、`cookies/`、`.env`、`config.yaml` 与 `sync_runs` 运行记录。
- **脱敏检查**：推送前确认 `catalog/*.csv`、`manifests/` 与两个 SQLite 库中不含盘符绝对路径（`C:\`、`D:\`）、用户名、Cookie 字段或私密服务端 URL。
