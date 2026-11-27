# 维护与发版 SOP

本文档面向仓库维护者，说明如何从上游工具产物打包并发布新的日期版本快照（`vYYYY.MM.DD`）。

## 1. 上游工具链

- **ASR 转录流水线**：[chosenecho/qwen3-asr-pipeline](https://github.com/chosenecho/qwen3-asr-pipeline)
- **多平台内容归档器**：[chosenecho/biliyt-archiver](https://github.com/chosenecho/biliyt-archiver)（后续接入动态、专栏、视频元数据、评论与图片）

## 2. 发布新的 ASR 日期快照

### 第一步：本地打包与目录更新

```bash
python scripts/package_asr.py package \
  --tag v2026.11.27 \
  --asr-root "C:\WorkSpace\asr" \
  --update-repo-metadata
```

该命令会自动完成：
1. 按白名单（仅 `bili_23191782` 与 `yt_wujixuanshi`）校验并扫描转录文件；
2. 导出 WAL 合并、移除服务端 URL / 本机绝对路径的脱敏 `asr_pipeline.db`；
3. 按层生成 UTF-8 PAX `.tar.gz` 分包到 `release_dist/<tag>/`；
4. 生成 `MANIFEST.json` 与 `SHA256SUMS.txt`，并同步更新仓库内的 `catalog/` 与 `manifests/<tag>/`。

### 第二步：提交元数据并推送

```bash
git status -sb
git add catalog/ manifests/ README.md
git commit -m "chore(release): record ASR snapshot v2026.11.27"
git push
```

### 第三步：创建并上传 GitHub Release

```bash
python scripts/package_asr.py publish \
  --tag v2026.11.27 \
  --skip-package \
  --clean
```

### 第四步：端到端验收

在临时目录执行用户视角还原测试，验证哈希校验与解压无误：

```bash
python scripts/restore.py --tag v2026.11.27 --preset light --dest ./_verify_tmp
```

## 3. 安全检查红线

- 严禁将 `cookies/`、`.env`、`config.yaml` 或任何其他创作者（非 `bili_23191782` / `yt_wujixuanshi`）的数据带入本仓库或 Release 资产。
- 每次发版前确认 `asr_pipeline.db` 中无本机盘符绝对路径与私密服务端 URL。
