# AI 短剧下载优化计划书

## 目标

把 telegram-downloader 从通用 Telegram 文件下载器，优化成适合“短剧频道归档”的自托管工具：能稳定监控频道、自动下载视频/图片、保留中文命名，并支持对历史下载文件做安全重命名。

## 当前已完成基础

- Fork 分支：`feat/ai-short-drama-web-config`
- 已实现：
  - Pyrogram 原生代理配置：`socks5` / `http`
  - 家里 WARP 节点 compose 示例：`socks5://your-proxy-host:40000`
  - Telegram 原生 `photo` 下载
  - `.mp4,.jpg,.jpeg,.png,.webp` 过滤
  - 中文文件名/目录名保留
  - healthcheck UTC/本地时区误报修复
  - FastAPI + 静态 HTML Web 配置面板
  - `/api/config`、`/api/status`、`/api/restart`

## 本轮优化范围

### 1. 短剧目录解析

频道常见格式：先发一条图片/目录消息，caption 中列出多集：

```text
美丽新世界 EP-1 樱花道偶遇
美丽新世界 EP-2 误入厕所成变态
美丽新世界 EP-3 烂醉如泥的邻居美眉
```

实现：

- 新增 `parse_caption_episodes(caption)`
- 支持一条 caption 解析多条 EP
- 兼容格式：
  - `EP-1`
  - `EP1`
  - `EP 1`
  - 前缀 emoji，如 `🔥美丽新世界 EP-1 ...`
- 标准化 episode：`EP-1`、`EP-12`

### 2. 标准短剧文件名生成

新增：

```python
build_episode_filename(episode, ext)
```

命名规则：

```text
美丽新世界_EP01_樱花道偶遇.mp4
美丽新世界_EP02_误入厕所成变态.mp4
```

要求：

- 中文保留
- EP 编号补零到两位
- 危险字符清理
- 扩展名保持原文件类型

### 3. 离线重命名工具

新增命令：

```bash
python -m src.tools.short_drama_renamer --help
```

核心参数：

```bash
python -m src.tools.short_drama_renamer \
  --downloads-dir /vol3/1000/downloads/AI短剧 \
  --catalog-caption-file /tmp/catalog.txt
```

默认 dry-run，只输出计划：

```text
old.mp4 -> 美丽新世界_EP01_樱花道偶遇.mp4
```

真正执行：

```bash
python -m src.tools.short_drama_renamer \
  --downloads-dir /vol3/1000/downloads/AI短剧 \
  --catalog-caption-file /tmp/catalog.txt \
  --apply
```

### 4. 重命名匹配策略

优先级：

1. 如果提供 `state.db` 且能匹配 `download_history`，按 `message_id` 升序排列视频。
2. 否则按文件修改时间排序。
3. 再 fallback 到文件名排序。

只处理视频文件，默认：

```text
.mp4
```

图片/目录图暂不强制匹配到 EP，可跳过或按目录图规则命名。

### 5. 冲突处理

如果目标文件已存在：

```text
美丽新世界_EP01_樱花道偶遇.mp4
美丽新世界_EP01_樱花道偶遇_2.mp4
美丽新世界_EP01_樱花道偶遇_3.mp4
```

必须避免覆盖已有文件。

### 6. 测试

新增测试：

- 多行中文 caption 解析
- EP 文件名生成
- dry-run 重命名计划
- 冲突文件自动加后缀
- 不依赖 Pyrogram，保证当前环境可跑

## 推进步骤

1. 扩展 `src/media.py`：多 EP caption 解析和标准文件名生成。
2. 新增 `src/tools/short_drama_renamer.py`。
3. 新增单元测试。
4. 更新 README，写明用法。
5. 跑目标测试。
6. commit + push 到当前 fork 分支。
7. 在 NAS 上先 dry-run 验证重命名计划，确认后再 apply。

## 不做事项

- 不增加 `https` 代理 scheme；代理保持 `socks5` / `http`。
- 不直接改 NAS 正在运行的服务。
- 不提交任何真实 API key、手机号、session、state.db。
- 不自动重命名 NAS 上 51G 文件，必须先 dry-run 预览。
