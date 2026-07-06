# OCR Organizer Workflow

The OCR organizer is an optional post-download workflow for AI short drama
videos. The downloader keeps the original download directory unchanged and can
create a separate hardlink view after an external OCR or manual review step.

Default behavior is unchanged: `ocr_organizer.enabled` is `false`, videos remain
under the configured download root, and the downloader does not rename, modify,
or delete original files.

## Configuration

```yaml
ocr_organizer:
  enabled: false
  cover_cache_dir: /app/.sessions/ocr_covers
  output_dir: /downloads_ocr
  min_confidence: 0.8
```

When enabled, the downloader extracts cover candidates after a successful batch
that downloaded videos. Failures are logged as non-fatal and do not stop
downloads.

## Cover Candidates

The extractor looks for cover images near each video message:

- It calls `get_media_group()` and prefers same-group photos.
- It also extracts the video thumbnail fallback when available.
- Each message gets a cache folder with a `manifest.json`.

Manual extraction:

```bash
python -m src.ocr_organizer extract-covers \
  --config /app/config.yaml \
  --source-index 0 \
  --output-dir /app/.sessions/ocr_covers \
  --min-message-id 1619
```

## OCR Map

OCR remains external and pluggable. The map can come from Hermes vision,
Tesseract, or manual review. Use JSON keyed by Telegram `message_id`:

```json
{
  "1619": {
    "title": "美丽新世界",
    "episode": "1",
    "confidence": 0.97
  }
}
```

## Apply Map

`apply-map` reads `download_history`, resolves downloaded files using the same
filename sanitizer as the downloader, and creates a separate hardlink folder.
It writes `_hardlink_plan.json`, `_skipped.json`, and `_missing.json`.

```bash
python -m src.ocr_organizer apply-map \
  --download-root /downloads \
  --state-db /app/.sessions/state.db \
  --ocr-map /path/map.json \
  --cover-root /app/.sessions/ocr_covers \
  --output-root /downloads_ocr \
  --min-confidence 0.8
```

Use `--dry-run` to write the plan files without creating links.

## NAS Example

Original downloads:

```text
/vol3/1000/downloads/AI短剧
```

OCR hardlink view:

```text
/vol3/1000/downloads/AI短剧_OCR整理_<timestamp>
```

## Safe Deletion

Because the organized folder uses hardlinks, deleting an item from the OCR view
does not delete the original path entry. The file data remains as long as at
least one hardlink exists. To remove media completely, delete both the original
downloaded file and any organized hardlink copies.
