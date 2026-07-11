# Telegram Media Downloader

[![CI](https://github.com/rfsbraz/telegram-downloader/actions/workflows/ci.yml/badge.svg)](https://github.com/rfsbraz/telegram-downloader/actions/workflows/ci.yml)
[![Docker](https://github.com/rfsbraz/telegram-downloader/actions/workflows/docker.yml/badge.svg)](https://github.com/rfsbraz/telegram-downloader/actions/workflows/docker.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker Pulls](https://img.shields.io/docker/pulls/rfsbraz/telegram-downloader)](https://hub.docker.com/r/rfsbraz/telegram-downloader)

A self-hosted daemon that automatically downloads media from Telegram channels, groups, and forum topics. Configure once, run forever.

[**Documentation**](https://rfsbraz.github.io/telegram-downloader/) | [**Quick Start**](#quick-start) | [**Examples**](#examples)

## Why?

Telegram is great for sharing files, but manually downloading from multiple sources is tedious. This tool runs in the background and:

- Downloads new files as they're posted
- Filters by extension, size, date, or filename pattern
- Organizes files into folders by source
- Skips duplicates automatically
- Tracks downloads persistently so files aren't re-downloaded after being moved or renamed
- Sends notifications when downloads complete

## Features

| Feature | Description |
|---------|-------------|
| **Multi-Source** | Channels, groups, supergroups, forum topics, private chats |
| **Smart Filtering** | By extension, file size, date range, filename patterns |
| **Photos & Video** | Native Telegram photos plus document/video/audio media |
| **Auto-Organization** | Per-source folders, duplicate detection, conflict resolution, persistent download tracking |
| **Daemon Mode** | Runs continuously with configurable check intervals |
| **Web Config** | Optional FastAPI panel for editing the YAML config |
| **Notifications** | Discord webhooks, generic HTTP POST |
| **Docker Native** | Multi-arch images (amd64/arm64), health checks, non-root |

## Quick Start

### Prerequisites

- Docker and Docker Compose
- Telegram API credentials from [my.telegram.org/apps](https://my.telegram.org/apps)
- Telegram account with access to the channels/groups you want to download from

<img src=".github/images/channel-url.png" alt="Where to find the channel URL" width="400">

### Setup

```bash
mkdir telegram-downloader && cd telegram-downloader

cat > docker-compose.yml << 'EOF'
services:
  telegram-downloader:
    image: rfsbraz/telegram-downloader:latest
    restart: unless-stopped
    environment:
      - TDL_API_ID=YOUR_API_ID
      - TDL_API_HASH=YOUR_API_HASH
      - TDL_PHONE_NUMBER=+1234567890
      - TDL_DAEMON_ENABLED=true
      - TDL_DAEMON_CHECK_INTERVAL=300
      - TDL_SOURCES_0_URL=https://t.me/example_channel
    volumes:
      - ./downloads:/downloads
      - ./sessions:/app/.sessions
    stdin_open: true
    tty: true
EOF

docker compose run --rm telegram-downloader
```

On first run, you'll be prompted for the Telegram verification code sent to your phone. After authentication, restart with `docker compose up -d` for daemon mode.

## Configuration

All configuration is done via environment variables with the `TDL_` prefix. See the [full configuration reference](https://rfsbraz.github.io/telegram-downloader/configuration/) for all options.

### Sources

```yaml
# Public channel
- TDL_SOURCES_0_URL=https://t.me/channel_name

# Private channel/group (use the t.me/c/ link format)
- TDL_SOURCES_1_URL=https://t.me/c/1234567890/1

# Forum topic (chat_id/topic_id)
- TDL_SOURCES_2_URL=https://t.me/c/1234567890/123
```

### Filters

```yaml
# File extensions (comma-separated)
- TDL_SOURCES_0_FILTERS_EXTENSIONS=.pdf,.epub,.mobi

# Size limits
- TDL_SOURCES_0_FILTERS_MIN_SIZE=100KB
- TDL_SOURCES_0_FILTERS_MAX_SIZE=500MB

# Global defaults (apply to all sources)
- TDL_GLOBAL_FILTERS_EXTENSIONS=.pdf,.epub

# Short-drama media with native Telegram photos
- TDL_GLOBAL_FILTERS_EXTENSIONS=.mp4,.jpg,.jpeg,.png,.webp
- TDL_GLOBAL_FILTERS_ONLY_AFTER=2026-06-18T00:00:00+08:00
- TDL_GLOBAL_FILTERS_ONLY_BEFORE=2026-07-01T00:00:00+08:00
```

Short-drama catalog captions with multiple episode lines are recognized:

```text
美丽新世界 EP-1 樱花道偶遇
美丽新世界 EP-2 误入厕所成变态
```

Episode filenames are normalized as `美丽新世界_EP01_樱花道偶遇.mp4`, preserving Chinese text while cleaning unsafe filesystem characters.

To safely rename historical downloads from a catalog caption, save the caption text and run the offline renamer. It defaults to dry-run:

```bash
python -m src.tools.short_drama_renamer \
  --downloads-dir /vol3/1000/downloads/AI短剧 \
  --catalog-caption-file /tmp/catalog.txt
```

When the plan looks correct, apply it:

```bash
python -m src.tools.short_drama_renamer \
  --downloads-dir /vol3/1000/downloads/AI短剧 \
  --catalog-caption-file /tmp/catalog.txt \
  --apply
```

If you have the downloader state database, pass `--state-db /path/to/state.db` to order videos by Telegram `message_id`; otherwise files are ordered by modification time with filename as the fallback. Existing target files are never overwritten; conflicts receive `_2`, `_3`, and so on.

At runtime, short-drama catalog posts are detected from multi-line captions/text such as `剧名 EP-1 标题`. The active catalog is persisted in `state.db` per source, and following video/document messages are named from the channel-posted series and episode data, for example `美丽新世界/美丽新世界_EP01_樱花道偶遇.mp4`. `source.name` is only the display/folder fallback when no runtime catalog has been inferred.

### Daemon Mode

```yaml
- TDL_DAEMON_ENABLED=true
- TDL_DAEMON_CHECK_INTERVAL=300  # seconds between checks
```

### Notifications

```yaml
- TDL_NOTIFICATIONS_ENABLED=true
- TDL_NOTIFICATIONS_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
- TDL_NOTIFICATIONS_DETAIL_LEVEL=summary  # minimal, summary, or detailed
```

### Docker Environment

```yaml
# User/group ID for file permissions (arr-stack compatible)
- PUID=1000
- PGID=1000

# Timezone
- TZ=Europe/Lisbon

# Store all files in download_dir without per-channel subfolders
- TDL_FLAT_STRUCTURE=true

# Optional fallback folder/display name when no runtime catalog is active
- TDL_SOURCES_0_NAME=美丽新世界

# Persistent download tracking (enabled by default)
# When enabled, files won't be re-downloaded after being moved/renamed
- TDL_TRACK_DOWNLOADS=true
```

### MTProto Proxy

Pyrogram does not use generic `HTTP_PROXY`/`HTTPS_PROXY` variables for MTProto traffic. Configure the client proxy with `TDL_PROXY_*`:

```yaml
- TDL_PROXY_ENABLED=true
- TDL_PROXY_SCHEME=socks5
- TDL_PROXY_HOST=your-proxy-host
- TDL_PROXY_PORT=40000
# Optional:
- TDL_PROXY_USERNAME=
- TDL_PROXY_PASSWORD=
```

The default home WARP SOCKS5 example is `socks5://your-proxy-host:40000`.

### Web Config Panel

Run the optional panel with Uvicorn:

```bash
uvicorn src.web.app:app --host 127.0.0.1 --port 8080
```

Endpoints:

- `GET /` static configuration page
- `GET /api/config` returns redacted YAML config as JSON
- `POST /api/config` validates and atomically writes YAML
- `GET /api/status` returns health, cursor/pending state, and recent logs when available
- `POST /api/restart` returns a restart reminder

Set `TDL_CONFIG_FILE=/app/config.yaml` to choose the YAML path. Set `TDL_WEB_TOKEN` when exposing the panel beyond localhost; use `Authorization: Bearer <token>`. Any `TDL_` environment variables still override YAML at runtime, so the panel shows a warning when overrides are present.

## Examples

Complete Docker Compose configurations for common use cases:

| Example | Description |
|---------|-------------|
| [ebook-forum.yml](examples/ebook-forum.yml) | Download ebooks from forum topics |
| [youtube-archiver.yml](examples/youtube-archiver.yml) | Archive YouTube mirror channels |
| [news-aggregator.yml](examples/news-aggregator.yml) | Aggregate media from news channels |
| [personal-backup.yml](examples/personal-backup.yml) | Backup your saved messages |
| [docker-compose.example.yml](docker-compose.example.yml) | Downloader plus Web panel, sessions, config volume, and WARP SOCKS5 proxy |

## Docker Images

Available from Docker Hub and GitHub Container Registry:

```bash
# Docker Hub
docker pull rfsbraz/telegram-downloader:latest

# GitHub Container Registry
docker pull ghcr.io/rfsbraz/telegram-downloader:latest
```

| Tag | Description |
|-----|-------------|
| `latest` | Latest stable release |
| `edge` | Latest main branch (may be unstable) |
| `v1.2.3` | Specific version |
| `v1.2` | Latest patch for minor version |
| `v1` | Latest minor for major version |
| `sha-abc1234` | Specific commit |

Multi-arch: `linux/amd64` and `linux/arm64` (Raspberry Pi 4+, Apple Silicon, AWS Graviton).

## Troubleshooting

| Error | Solution |
|-------|----------|
| `PEER_ID_INVALID` | The chat ID is invalid or you haven't joined the chat. Open it in Telegram first. |
| `FloodWait X` | Rate limited by Telegram. The daemon waits automatically. Increase check interval if persistent. |
| `AUTH_KEY_UNREGISTERED` | Session expired. Delete `./sessions/` and re-authenticate. |
| Health check failing | Check logs with `docker compose logs -f` |

See the [troubleshooting guide](https://rfsbraz.github.io/telegram-downloader/troubleshooting/) for more.

## Development

```bash
git clone https://github.com/rfsbraz/telegram-downloader.git
cd telegram-downloader
pip install -r requirements.txt
```

```bash
make test          # all tests with coverage
make unit          # unit tests only
make integration   # integration tests only
make build         # build Docker image
make run           # build + docker compose up
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for full development guidelines.

## License

MIT - see [LICENSE](LICENSE) for details.

## Acknowledgments

- [Pyrogram](https://docs.pyrogram.org/) - Telegram MTProto API framework
- [Pydantic](https://docs.pydantic.dev/) - Data validation
- [docker/metadata-action](https://github.com/docker/metadata-action) - Docker tagging

## ☕ Support

If you find this useful and want to support development, you can [buy me a coffee](https://buymeacoffee.com/rfsbraz) - no pressure at all, just a nice way to say thanks.
