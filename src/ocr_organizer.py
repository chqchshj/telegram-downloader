"""Optional OCR-map organizer for short-drama downloads.

This module does not perform OCR.  It extracts cover candidates for external
OCR tooling and can apply a reviewed OCR map into a separate hardlink view.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import mimetypes
import json
import os
import re
import shutil
import sqlite3
from collections import Counter
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from src.media import is_short_drama_episode_media
from src.nfo import write_organized_episode_nfo
from src.security.sanitizer import sanitize_filename, validate_path_safety


VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
COVER_SUFFIX = ".jpg"
OCR_MAP_AUTO_FILE = "ocr_map_auto.json"
OCR_REVIEW_QUEUE_FILE = "ocr_review_queue.json"
OCR_VERIFY_RAW_FILE = "ocr_verify_raw.json"
DEFAULT_LLM_BASE_URL = "http://192.168.2.3:8318/v1"
DEFAULT_OCR_BACKFILL_RECENT_LIMIT = 50


@dataclass(frozen=True)
class HistoryRow:
    """Downloaded media row used by the organizer."""

    file_unique_id: str
    file_name: str
    file_size: int
    source_key: str
    message_id: int
    downloaded_at: str | None = None


@dataclass(frozen=True)
class PlanItem:
    """A planned or completed hardlink-view item."""

    message_id: int
    title: str
    episode: str | None
    source: str
    target: str
    cover: str | None = None
    action: str = "link"


@dataclass(frozen=True)
class OcrBackfillSelection:
    """Bounded OCR backfill selection result."""

    candidate_count: int
    selected_message_ids: list[int]
    skipped: dict[str, int]


def normalize_episode_suffix(episode: str | int | None) -> str:
    """Normalize episode input to ``EP01`` style suffixes."""
    if episode is None:
        return ""
    text = str(episode).strip()
    if not text:
        return ""
    match = re.search(r"\d+", text)
    if match:
        return f"EP{int(match.group(0)):02d}"
    return sanitize_filename(text).upper()


def normalize_title(title: str | None) -> str:
    """Normalize OCR title text for safe path components."""
    return sanitize_filename((title or "").strip())


def build_ocr_filename(
    title: str,
    episode: str | int | None,
    message_id: int,
    suffix: str,
) -> tuple[str, str]:
    """Return relative ``(folder, filename)`` components for an OCR item."""
    safe_title = normalize_title(title)
    episode_suffix = normalize_episode_suffix(episode)
    ext = suffix if suffix.startswith(".") else f".{suffix}"

    parts = [safe_title]
    if episode_suffix:
        parts.append(episode_suffix)
    filename = sanitize_filename("_".join(parts) + ext)
    return safe_title, filename


def _candidate_paths(
    source_dir: Path,
    sanitized_name: str,
    original_suffix: str,
) -> Iterable[Path]:
    yield source_dir / sanitized_name
    stem = Path(sanitized_name).stem
    suffix = Path(sanitized_name).suffix or original_suffix
    for idx in range(2, 1000):
        yield source_dir / f"{stem}_{idx}{suffix}"


def resolve_downloaded_media_path(
    download_root: str | Path,
    source_folder: str | None,
    file_name: str,
    file_size: int,
) -> Path | None:
    """Resolve a history row to the downloaded file using downloader semantics."""
    root = Path(download_root)
    safe_name = sanitize_filename(file_name)
    original_suffix = Path(file_name).suffix

    source_dirs: list[Path]
    if source_folder:
        source_dirs = [root / sanitize_filename(source_folder)]
    else:
        source_dirs = [root]

    for source_dir in source_dirs:
        for candidate in _candidate_paths(source_dir, safe_name, original_suffix):
            if candidate.exists() and candidate.is_file() and candidate.stat().st_size == file_size:
                return candidate
            if candidate.name == safe_name and not candidate.exists():
                break

    search_roots = source_dirs if source_folder else [root]
    for search_root in search_roots:
        if not search_root.exists():
            continue
        for candidate in search_root.rglob(f"*{Path(safe_name).suffix}"):
            if candidate.is_file() and candidate.stat().st_size == file_size:
                return candidate
    return None


async def extract_cover_candidates(
    client: Any,
    chat_id: int | str,
    message_id: int,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Download media-group photo covers and video thumbnail fallback candidates."""
    destination = Path(output_dir) / str(message_id)
    destination.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "message_id": message_id,
        "chat_id": chat_id,
        "candidates": [],
    }

    group_messages = []
    try:
        group_messages = await client.get_media_group(chat_id, message_id)
    except Exception:
        group_messages = []

    photo_index = 0
    for group_msg in group_messages or []:
        if getattr(group_msg, "photo", None):
            photo_index += 1
            target = destination / f"group_photo_{photo_index}{COVER_SUFFIX}"
            downloaded = await client.download_media(group_msg, file_name=str(target))
            if downloaded:
                manifest["candidates"].append({
                    "kind": "group_photo",
                    "path": str(Path(downloaded)),
                    "preferred": True,
                })

    video_msg = await client.get_messages(chat_id=chat_id, message_ids=message_id)
    video = getattr(video_msg, "video", None) or getattr(video_msg, "document", None)
    thumbs = getattr(video, "thumbs", None) or []
    if thumbs:
        target = destination / f"video_thumb_1{COVER_SUFFIX}"
        downloaded = await client.download_media(thumbs[0], file_name=str(target))
        if downloaded:
            manifest["candidates"].append({
                "kind": "video_thumb",
                "path": str(Path(downloaded)),
                "preferred": not any(c["kind"] == "group_photo" for c in manifest["candidates"]),
            })

    manifest_path = destination / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def _load_history_rows(state_db: str | Path) -> list[HistoryRow]:
    connection = sqlite3.connect(state_db)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT file_unique_id, file_name, file_size, source_key, message_id, downloaded_at
            FROM download_history
            ORDER BY message_id
            """
        ).fetchall()
    finally:
        connection.close()
    return [HistoryRow(**dict(row)) for row in rows]


def load_recent_video_history_rows(
    state_db: str | Path,
    *,
    limit: int = DEFAULT_OCR_BACKFILL_RECENT_LIMIT,
    source_key: str | None = None,
) -> list[HistoryRow]:
    """Load newest bounded video-looking rows from download_history."""
    state_path = Path(state_db)
    if not state_path.exists():
        return []

    bounded_limit = max(1, int(limit or DEFAULT_OCR_BACKFILL_RECENT_LIMIT))
    suffixes = sorted(VIDEO_SUFFIXES)
    suffix_clause = " OR ".join("LOWER(file_name) LIKE ?" for _ in suffixes)
    where = [f"({suffix_clause})"]
    params: list[Any] = [f"%{suffix}" for suffix in suffixes]
    if source_key:
        where.append("source_key = ?")
        params.append(source_key)
    params.append(bounded_limit)

    connection = sqlite3.connect(state_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            f"""
            SELECT file_unique_id, file_name, file_size, source_key, message_id, downloaded_at
            FROM download_history
            WHERE {' AND '.join(where)}
            ORDER BY message_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            rows = []
        else:
            raise
    finally:
        connection.close()
    return [HistoryRow(**dict(row)) for row in rows]


def _coerce_history_rows(rows: Iterable[HistoryRow | dict[str, Any]]) -> list[HistoryRow]:
    coerced = []
    for row in rows:
        if isinstance(row, HistoryRow):
            coerced.append(row)
        else:
            coerced.append(HistoryRow(**row))
    return coerced


def _load_json(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    path = Path(path)
    if path.is_dir():
        manifest: dict[str, Any] = {}
        for manifest_path in path.glob("*/manifest.json"):
            data = _load_json(manifest_path)
            message_id = data.get("message_id")
            if message_id is not None:
                manifest[str(message_id)] = data
        return manifest
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def _load_json_value(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _coerce_message_id(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _message_id_sort_key(message_id: str) -> tuple[int, int | str]:
    try:
        return (0, int(message_id))
    except ValueError:
        return (1, message_id)


def _load_ocr_map(path: str | Path) -> dict[str, dict[str, Any]]:
    try:
        data = _load_json_value(path)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        message_id = _coerce_message_id(key)
        if message_id is not None and isinstance(value, dict):
            rows[str(message_id)] = value
    return rows


def _load_message_rows(path: str | Path) -> dict[str, dict[str, Any]]:
    try:
        data = _load_json_value(path)
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict):
        items = data.values()
    elif isinstance(data, list):
        items = data
    else:
        return {}

    rows: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        message_id = _coerce_message_id(item.get("message_id"))
        if message_id is not None:
            rows[str(message_id)] = item
    return rows


def _ordered_message_rows(rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [rows[key] for key in sorted(rows, key=_message_id_sort_key)]


def _load_verified_message_ids(cover_root: str | Path) -> dict[str, set[int]]:
    root = Path(cover_root)
    accepted = {_coerce_message_id(key) for key in _load_ocr_map(root / OCR_MAP_AUTO_FILE)}
    review = {_coerce_message_id(key) for key in _load_message_rows(root / OCR_REVIEW_QUEUE_FILE)}
    raw = {_coerce_message_id(key) for key in _load_message_rows(root / OCR_VERIFY_RAW_FILE)}
    return {
        "verified_accepted": {message_id for message_id in accepted if message_id is not None},
        "verified_review": {message_id for message_id in review if message_id is not None},
        "verified_raw": {message_id for message_id in raw if message_id is not None},
    }


def _verified_skip_reason(verified: dict[str, set[int]], message_id: int) -> str | None:
    for reason in ("verified_accepted", "verified_review", "verified_raw"):
        if message_id in verified.get(reason, set()):
            return reason
    return None


def _manifest_candidate_state(cover_root: str | Path, message_id: int) -> str:
    manifest_path = Path(cover_root) / str(message_id) / "manifest.json"
    if not manifest_path.exists():
        return "missing"
    try:
        data = _load_json_value(manifest_path)
    except (OSError, json.JSONDecodeError):
        return "corrupt"
    if not isinstance(data, dict):
        return "corrupt"
    candidates = data.get("candidates", [])
    return "candidates" if candidates else "no_candidates"


def _output_plan_paths(output_root: str | Path | None) -> list[Path]:
    if not output_root:
        return []
    root = Path(output_root)
    if not root.exists():
        return []
    paths: list[Path] = []
    direct = root / "_hardlink_plan.json"
    if direct.exists():
        paths.append(direct)
    paths.extend(
        path
        for path in root.glob("auto_*/_hardlink_plan.json")
        if path.is_file()
    )
    return paths


def _load_output_plan_message_ids(output_root: str | Path | None) -> set[int]:
    message_ids: set[int] = set()
    for plan_path in _output_plan_paths(output_root):
        try:
            data = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = data if isinstance(data, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                message_ids.add(int(row["message_id"]))
            except (KeyError, TypeError, ValueError):
                continue
    return message_ids


def select_ocr_backfill_message_ids(
    recent_history_rows: Iterable[HistoryRow | dict[str, Any]],
    *,
    cover_root: str | Path,
    download_root: str | Path,
    output_root: str | Path | None = None,
    source_key: str | None = None,
    exclude_message_ids: Iterable[int] | None = None,
) -> OcrBackfillSelection:
    """Select recent downloaded videos that still need OCR cover processing."""
    rows = _coerce_history_rows(recent_history_rows)
    skipped: Counter[str] = Counter()
    selected: list[int] = []
    seen: set[int] = set()
    excluded = {int(message_id) for message_id in (exclude_message_ids or [])}
    output_message_ids = _load_output_plan_message_ids(output_root)
    verified_message_ids = _load_verified_message_ids(cover_root)

    for row in rows:
        if source_key and row.source_key != source_key:
            skipped["other_source"] += 1
            continue
        if row.message_id in seen:
            skipped["duplicate_message_id"] += 1
            continue
        seen.add(row.message_id)
        if row.message_id in excluded:
            skipped["fresh_batch"] += 1
            continue
        if Path(row.file_name).suffix.lower() not in VIDEO_SUFFIXES:
            skipped["not_video"] += 1
            continue

        manifest_state = _manifest_candidate_state(cover_root, row.message_id)
        if row.message_id in output_message_ids:
            skipped["existing_output_plan"] += 1
            continue
        verified_reason = _verified_skip_reason(verified_message_ids, row.message_id)
        if verified_reason:
            skipped[verified_reason] += 1
            continue
        if manifest_state == "candidates":
            skipped["manifest_candidates"] += 1
            continue
        if manifest_state == "no_candidates":
            skipped["manifest_no_candidates"] += 1
            continue

        source = resolve_downloaded_media_path(
            download_root,
            None,
            row.file_name,
            row.file_size,
        )
        if source is None:
            skipped["missing_file"] += 1
            continue
        if source.suffix.lower() not in VIDEO_SUFFIXES:
            skipped["not_video"] += 1
            continue
        selected.append(row.message_id)

    return OcrBackfillSelection(
        candidate_count=len(rows),
        selected_message_ids=sorted(selected),
        skipped=dict(sorted(skipped.items())),
    )


def select_preferred_cover_image(manifest: dict[str, Any]) -> Path | None:
    """Select the preferred cover image from a cover manifest."""
    candidates = manifest.get("candidates", []) if isinstance(manifest, dict) else []
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("preferred") and candidate.get("path"):
            return Path(candidate["path"])
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("path"):
            return Path(candidate["path"])
    return None


def _parse_llm_json_content(content: str) -> dict[str, Any]:
    """Parse a JSON object from model text, including fenced code blocks."""
    text = (content or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("LLM response JSON must be an object")
    return data


def _resolve_llm_base_url(base_url: str | None) -> str:
    return (base_url or os.getenv("OCR_LLM_BASE_URL") or DEFAULT_LLM_BASE_URL).rstrip("/")


def _resolve_llm_api_key(api_key: str | None = None, api_key_env: str | None = None) -> str | None:
    if api_key:
        return api_key
    env_names = [api_key_env] if api_key_env else []
    env_names.extend(["TDL_OCR_ORGANIZER_LLM_API_KEY", "CPA_API_KEY"])
    for env_name in env_names:
        if env_name and os.getenv(env_name):
            return os.getenv(env_name)
    return None


def _image_data_url(path: Path) -> str:
    media_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def call_llm_vision_endpoint(
    cover_path: str | Path,
    *,
    message_id: int,
    base_url: str | None,
    api_key: str | None,
    model: str,
    timeout: int = 60,
    max_retries: int = 3,
    retry_delay: float = 5.0,
) -> dict[str, Any]:
    """Call an OpenAI-compatible vision endpoint for one cover image."""
    url = f"{_resolve_llm_base_url(base_url)}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    prompt = (
        "You verify short-drama cover OCR. Return only JSON with keys: "
        "title string, episode string or null, is_drama boolean, "
        "confidence number 0..1, reason string. Be conservative."
    )
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": _image_data_url(Path(cover_path))}},
                ],
            }
        ],
        "temperature": 0,
    }
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            data = _parse_llm_json_content(content)
            return _normalize_llm_result(data, message_id=message_id, cover_path=cover_path, status="model_ok")
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))
            else:
                raise
    raise RuntimeError(f"LLM call failed after {max_retries} retries: {last_error}")


def _normalize_llm_result(
    data: dict[str, Any],
    *,
    message_id: int,
    cover_path: str | Path | None,
    status: str,
) -> dict[str, Any]:
    try:
        confidence = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "message_id": int(data.get("message_id") or message_id),
        "title": str(data.get("title") or "").strip(),
        "episode": data.get("episode"),
        "is_drama": bool(data.get("is_drama")),
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(data.get("reason") or "").strip(),
        "cover_path": str(cover_path) if cover_path else None,
        "status": status,
    }


def _review_reason(
    result: dict[str, Any],
    *,
    min_confidence: float,
    require_episode: bool,
    reject_non_drama: bool,
) -> str | None:
    if result.get("status") != "model_ok":
        return result.get("status") or "model_error"
    if reject_non_drama and not result.get("is_drama"):
        return "not_drama"
    if not str(result.get("title") or "").strip():
        return "no_title"
    if float(result.get("confidence") or 0) < min_confidence:
        return "low_confidence"
    if require_episode and not normalize_episode_suffix(result.get("episode")):
        return "missing_episode"
    return None


def verify_cover_manifests(
    cover_root: str | Path,
    output_dir: str | Path,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    model: str = "gpt-5.5",
    min_confidence: float = 0.92,
    require_episode: bool = True,
    reject_non_drama: bool = True,
    message_ids: Iterable[int] | None = None,
) -> dict[str, Any]:
    """Verify cover manifests with the LLM and write accepted/review/raw files."""
    root = Path(cover_root)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    wanted = {int(mid) for mid in message_ids} if message_ids is not None else None
    accepted: dict[str, dict[str, Any]] = {}
    review: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []
    resolved_api_key = _resolve_llm_api_key(api_key, api_key_env)

    manifest_paths = sorted(root.glob("*/manifest.json")) if root.exists() else []
    for manifest_path in manifest_paths:
        try:
            manifest = _load_json(manifest_path)
            message_id = int(manifest.get("message_id"))
            if wanted is not None and message_id not in wanted:
                continue
            cover_path = select_preferred_cover_image(manifest)
            if cover_path is None or not cover_path.exists():
                result = _normalize_llm_result(
                    {"reason": "No cover image available"},
                    message_id=message_id,
                    cover_path=cover_path,
                    status="missing_cover",
                )
            else:
                result = call_llm_vision_endpoint(
                    cover_path,
                    message_id=message_id,
                    base_url=base_url,
                    api_key=resolved_api_key,
                    model=model,
                )
        except Exception as exc:
            fallback_id = int(manifest_path.parent.name) if manifest_path.parent.name.isdigit() else 0
            result = _normalize_llm_result(
                {"reason": str(exc)},
                message_id=fallback_id,
                cover_path=None,
                status="error",
            )

        reason = _review_reason(
            result,
            min_confidence=min_confidence,
            require_episode=require_episode,
            reject_non_drama=reject_non_drama,
        )
        raw.append(result)
        if reason is None:
            accepted[str(result["message_id"])] = {
                "title": result["title"],
                "episode": result["episode"],
                "confidence": result["confidence"],
                "is_drama": result["is_drama"],
                "reason": result["reason"],
                "cover_path": result["cover_path"],
            }
        else:
            review.append({**result, "review_reason": reason})

    map_path = output / OCR_MAP_AUTO_FILE
    review_path = output / OCR_REVIEW_QUEUE_FILE
    raw_path = output / OCR_VERIFY_RAW_FILE

    merged_accepted = _load_ocr_map(map_path)
    merged_review = _load_message_rows(review_path)
    merged_raw = _load_message_rows(raw_path)

    for result in raw:
        message_id = _coerce_message_id(result.get("message_id"))
        if message_id is not None:
            merged_raw[str(message_id)] = result
    for message_id, entry in accepted.items():
        merged_accepted[message_id] = entry
        merged_review.pop(message_id, None)
    for row in review:
        message_id = _coerce_message_id(row.get("message_id"))
        if message_id is None:
            continue
        message_key = str(message_id)
        merged_review[message_key] = row
        merged_accepted.pop(message_key, None)

    merged_accepted = dict(sorted(merged_accepted.items(), key=lambda item: _message_id_sort_key(item[0])))
    map_path.write_text(json.dumps(merged_accepted, ensure_ascii=False, indent=2), encoding="utf-8")
    review_path.write_text(json.dumps(_ordered_message_rows(merged_review), ensure_ascii=False, indent=2), encoding="utf-8")
    raw_path.write_text(json.dumps(_ordered_message_rows(merged_raw), ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "accepted": len(accepted),
        "review": len(review),
        "raw": len(raw),
        "accepted_total": len(merged_accepted),
        "review_total": len(merged_review),
        "raw_total": len(merged_raw),
        "ocr_map_auto_path": str(map_path),
        "ocr_review_queue_path": str(review_path),
        "ocr_verify_raw_path": str(raw_path),
    }


def auto_verify_and_apply(
    *,
    cover_root: str | Path,
    verify_output_dir: str | Path,
    download_root: str | Path,
    state_db: str | Path,
    output_dir: str | Path,
    auto_apply: bool,
    base_url: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    model: str = "gpt-5.5",
    min_confidence: float = 0.92,
    require_episode: bool = True,
    reject_non_drama: bool = True,
    message_ids: Iterable[int] | None = None,
) -> dict[str, Any]:
    """Verify covers and optionally create a hardlink view in the configured output root."""
    verify = verify_cover_manifests(
        cover_root,
        verify_output_dir,
        base_url=base_url,
        api_key=api_key,
        api_key_env=api_key_env,
        model=model,
        min_confidence=min_confidence,
        require_episode=require_episode,
        reject_non_drama=reject_non_drama,
        message_ids=message_ids,
    )
    result: dict[str, Any] = {"verify": verify, "apply": None}
    if not auto_apply or verify["accepted"] == 0:
        return result

    target_output = Path(output_dir)
    rows = _load_history_rows(state_db)
    apply = create_hardlink_view(
        rows,
        verify["ocr_map_auto_path"],
        cover_root,
        download_root,
        target_output,
        min_confidence=min_confidence,
    )
    result["apply"] = {"output_root": str(target_output), **apply}
    return result


def _ocr_entry(ocr_map: dict[str, Any], message_id: int) -> dict[str, Any] | None:
    value = ocr_map.get(str(message_id), ocr_map.get(message_id))
    return value if isinstance(value, dict) else None


def _cover_for_message(cover_manifest: dict[str, Any], message_id: int) -> Path | None:
    direct = cover_manifest.get(str(message_id), cover_manifest.get(message_id))
    if isinstance(direct, str):
        return Path(direct)
    if isinstance(direct, dict):
        candidates = direct.get("candidates", [])
    else:
        candidates = cover_manifest.get("candidates", []) if cover_manifest.get("message_id") == message_id else []
    return select_preferred_cover_image({"candidates": candidates})


def _unique_target(path: Path, used_targets: set[Path] | None = None) -> Path:
    used_targets = used_targets or set()
    if not path.exists() and path not in used_targets:
        return path
    stem = path.stem
    suffix = path.suffix
    for idx in range(2, 1000):
        candidate = path.with_name(f"{stem}_{idx}{suffix}")
        if not candidate.exists() and candidate not in used_targets:
            return candidate
    raise RuntimeError(f"Cannot find available target name for {path}")


def _episode_to_int(episode: str | int | None) -> int | None:
    if episode is None:
        return None
    match = re.search(r"\d+", str(episode))
    return int(match.group(0)) if match else None


def _write_nfo_for_plan_item(item: PlanItem, dry_run: bool) -> tuple[str | None, str | None]:
    if dry_run:
        return None, None
    series_nfo, episode_nfo = write_organized_episode_nfo(
        Path(item.target),
        series=item.title,
        episode_number=_episode_to_int(item.episode),
        title=item.episode or Path(item.target).stem,
        message_id=item.message_id,
    )
    return (str(series_nfo) if series_nfo else None, str(episode_nfo) if episode_nfo else None)


def _copy_or_link_cover(source: Path, target: Path, dry_run: bool) -> None:
    if dry_run:
        return
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def create_hardlink_view(
    download_history_rows: Iterable[HistoryRow | dict[str, Any]],
    ocr_map: dict[str, Any] | str | Path,
    cover_manifest: dict[str, Any] | str | Path | None,
    download_root: str | Path,
    output_root: str | Path,
    source_folder: str | None = None,
    min_confidence: float = 0.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a separate hardlink folder from history rows and an OCR map."""
    rows = _coerce_history_rows(download_history_rows)
    ocr_data = _load_json(ocr_map) if isinstance(ocr_map, (str, Path)) else ocr_map
    cover_data = _load_json(cover_manifest) if isinstance(cover_manifest, (str, Path)) else (cover_manifest or {})
    output = Path(output_root)
    download = Path(download_root)
    if output.resolve() == download.resolve() or output.resolve().is_relative_to(download.resolve()):
        raise ValueError("output_root must be separate from download_root")

    plan: list[PlanItem] = []
    skipped: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    used_targets: set[Path] = set()

    for row in rows:
        entry = _ocr_entry(ocr_data, row.message_id)
        if not entry:
            skipped.append({"message_id": row.message_id, "reason": "no_ocr_entry"})
            continue
        title = str(entry.get("title") or "").strip()
        confidence = float(entry.get("confidence") or 0)
        if not title:
            skipped.append({"message_id": row.message_id, "reason": "no_title"})
            continue
        if confidence < min_confidence:
            skipped.append({
                "message_id": row.message_id,
                "reason": "low_confidence",
                "confidence": confidence,
            })
            continue

        source = resolve_downloaded_media_path(
            download,
            source_folder,
            row.file_name,
            row.file_size,
        )
        if source is None:
            missing.append({"message_id": row.message_id, "file_name": row.file_name})
            continue
        if source.suffix.lower() not in VIDEO_SUFFIXES:
            skipped.append({"message_id": row.message_id, "reason": "not_video"})
            continue

        folder, filename = build_ocr_filename(
            title,
            entry.get("episode"),
            row.message_id,
            source.suffix,
        )
        target = validate_path_safety(output, output / folder / filename)
        target = _unique_target(target, used_targets)
        used_targets.add(target)

        cover_path = _cover_for_message(cover_data, row.message_id)
        cover_target = None
        if cover_path and cover_path.exists():
            cover_target = target.with_suffix(cover_path.suffix or COVER_SUFFIX)

        plan.append(PlanItem(
            message_id=row.message_id,
            title=title,
            episode=normalize_episode_suffix(entry.get("episode")),
            source=str(source),
            target=str(target),
            cover=str(cover_target) if cover_target else None,
            action="dry-run" if dry_run else "link",
        ))

    output.mkdir(parents=True, exist_ok=True)
    plan_path = output / "_hardlink_plan.json"
    skipped_path = output / "_skipped.json"
    missing_path = output / "_missing.json"

    nfo_written: list[dict[str, str | None]] = []
    if not dry_run:
        for item in plan:
            source = Path(item.source)
            target = Path(item.target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                os.link(source, target)
            if item.cover:
                cover_source = _cover_for_message(cover_data, item.message_id)
                if cover_source and cover_source.exists():
                    _copy_or_link_cover(cover_source, Path(item.cover), dry_run=False)
            series_nfo, episode_nfo = _write_nfo_for_plan_item(item, dry_run=False)
            nfo_written.append({
                "message_id": str(item.message_id),
                "series_nfo": series_nfo,
                "episode_nfo": episode_nfo,
            })

    plan_path.write_text(json.dumps([asdict(item) for item in plan], ensure_ascii=False, indent=2), encoding="utf-8")
    skipped_path.write_text(json.dumps(skipped, ensure_ascii=False, indent=2), encoding="utf-8")
    missing_path.write_text(json.dumps(missing, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "planned": len(plan),
        "skipped": len(skipped),
        "missing": len(missing),
        "nfo_written": sum(1 for item in nfo_written if item.get("episode_nfo")),
        "plan_path": str(plan_path),
        "skipped_path": str(skipped_path),
        "missing_path": str(missing_path),
    }


async def run_extract_covers(args: argparse.Namespace) -> None:
    from src.client import create_client
    from src.config import load_config
    from src.config.source_parser import parse_sources

    cfg = load_config(args.config)
    client = create_client(cfg)
    async with client:
        sources_with_filters = await parse_sources(cfg, client)
        if args.source_index is not None:
            try:
                source = sources_with_filters[args.source_index][0]
            except IndexError:
                raise SystemExit(f"source index out of range: {args.source_index}")
        else:
            source = None
            for idx, source_config in enumerate(cfg.sources):
                if source_config.url == args.source_url:
                    source = sources_with_filters[idx][0]
                    break
            if source is None:
                raise SystemExit("source not found; use --source-index or matching --source-url")

        async for msg in source.iterate_new_media(args.min_message_id - 1):
            media = getattr(msg, "video", None) or getattr(msg, "document", None)
            if is_short_drama_episode_media(msg, media):
                await extract_cover_candidates(client, source.chat_id, msg.id, args.output_dir)


def run_apply_map(args: argparse.Namespace) -> None:
    rows = _load_history_rows(args.state_db)
    result = create_hardlink_view(
        rows,
        args.ocr_map,
        args.cover_root,
        args.download_root,
        args.output_root,
        source_folder=args.source_folder,
        min_confidence=args.min_confidence,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def generate_nfo_for_plan_root(plan_root: str | Path) -> dict[str, Any]:
    root = Path(plan_root)
    plan_path = root / "_hardlink_plan.json"
    rows = json.loads(plan_path.read_text(encoding="utf-8"))
    written = []
    skipped = []
    for row in rows:
        if "target" not in row and "video" in row:
            row = {**row, "target": row["video"], "action": row.get("action", "link")}
        item = PlanItem(
            message_id=int(row["message_id"]),
            title=str(row["title"]),
            episode=row.get("episode"),
            source=str(row.get("source", "")),
            target=str(row["target"]),
            cover=row.get("cover"),
            action=str(row.get("action", "link")),
        )
        if not Path(item.target).exists():
            skipped.append({"message_id": item.message_id, "reason": "missing_target"})
            continue
        series_nfo, episode_nfo = _write_nfo_for_plan_item(item, dry_run=False)
        written.append({"message_id": item.message_id, "series_nfo": series_nfo, "episode_nfo": episode_nfo})
    return {"written": sum(1 for item in written if item.get("episode_nfo")), "processed": len(written), "skipped": len(skipped)}


def run_generate_nfo(args: argparse.Namespace) -> None:
    print(json.dumps(generate_nfo_for_plan_root(args.plan_root), ensure_ascii=False, indent=2))


def _parse_message_ids(value: str | None) -> list[int] | None:
    if not value:
        return None
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def run_llm_verify(args: argparse.Namespace) -> None:
    result = verify_cover_manifests(
        args.cover_root,
        args.output_dir,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        model=args.model,
        min_confidence=args.min_confidence,
        require_episode=args.require_episode,
        reject_non_drama=args.reject_non_drama,
        message_ids=_parse_message_ids(args.message_ids),
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OCR-map organizer for Telegram downloads")
    subcommands = parser.add_subparsers(dest="command", required=True)

    extract = subcommands.add_parser("extract-covers")
    extract.add_argument("--config", required=True)
    source_group = extract.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--source-url")
    source_group.add_argument("--source-index", type=int)
    extract.add_argument("--output-dir", required=True)
    extract.add_argument("--min-message-id", type=int, required=True)
    extract.set_defaults(func=lambda args: asyncio.run(run_extract_covers(args)))

    apply = subcommands.add_parser("apply-map")
    apply.add_argument("--download-root", required=True)
    apply.add_argument("--state-db", required=True)
    apply.add_argument("--ocr-map", required=True)
    apply.add_argument("--cover-root")
    apply.add_argument("--output-root", required=True)
    apply.add_argument("--source-folder")
    apply.add_argument("--min-confidence", type=float, default=0.0)
    apply.add_argument("--dry-run", action="store_true")
    apply.set_defaults(func=run_apply_map)

    verify = subcommands.add_parser("llm-verify")
    verify.add_argument("--cover-root", required=True)
    verify.add_argument("--output-dir", required=True)
    verify.add_argument("--base-url")
    verify.add_argument("--api-key-env", default="CPA_API_KEY")
    verify.add_argument("--model", default="gpt-5.5")
    verify.add_argument("--min-confidence", type=float, default=0.92)
    verify.add_argument("--require-episode", action=argparse.BooleanOptionalAction, default=True)
    verify.add_argument("--reject-non-drama", action=argparse.BooleanOptionalAction, default=True)
    verify.add_argument("--message-ids")
    verify.set_defaults(func=run_llm_verify)

    nfo = subcommands.add_parser("generate-nfo")
    nfo.add_argument("--plan-root", required=True)
    nfo.set_defaults(func=run_generate_nfo)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
