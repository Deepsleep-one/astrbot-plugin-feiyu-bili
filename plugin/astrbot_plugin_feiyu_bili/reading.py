"""Reading pipeline: one Bilibili part turned into paged, citable local evidence.

Flow: user names a BV (or one of its pages) -> official metadata -> subtitle with
real timestamps -> stored as a revision -> served to the model one small page at a
time. Nothing here calls a model and nothing guesses text: a part without subtitles
is reported as `subtitle_state="none"` with a reason, never filled in by hand.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
BVID = re.compile(r"^BV[0-9A-Za-z]{10}$")
_BVID_IN_TEXT = re.compile(r"(?<![0-9A-Za-z])BV[0-9A-Za-z]{10}(?![0-9A-Za-z])")
MAX_SEGMENT_CHARS = 2000
PAGE_SEGMENTS = 24
PAGE_CHARS = 2600
MIN_FRAME_GAP_S = 15.0
MAX_FRAMES = 40
FIRST_BATCH_FRAMES = 4


class ReadingError(Exception):
    """A reading request that cannot be honoured, with a model-readable reason."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def parse_target(text: str) -> tuple[str, int]:
    """Extract (bvid, page) from user text. Page defaults to 1 and is 1-based."""
    blob = str(text or "")
    found = _BVID_IN_TEXT.search(blob)
    if not found:
        raise ReadingError("bad_bvid", "没有在消息里找到有效的 BV 号。")
    page = 1
    for pattern in (r"(?:\?|&)p=(\d{1,3})", r"[Pp](\d{1,3})\b", r"第\s*(\d{1,3})\s*[Pp分]"):
        hit = re.search(pattern, blob)
        if hit:
            candidate = int(hit.group(1))
            if 1 <= candidate <= 999:
                page = candidate
                break
    return found.group(0), page


def clock_now() -> str:
    return datetime.now(TZ).isoformat()


def plan_frames(duration: float, want: int, *, limit: int = MAX_FRAMES,
                min_gap: float = MIN_FRAME_GAP_S) -> list[int]:
    """Evenly spaced frame times in ms, never closer than `min_gap` seconds apart."""
    duration = float(duration or 0)
    if duration <= 0:
        return [0]
    try:
        budget = int(want or 1)
    except (TypeError, ValueError):
        budget = 1
    want = max(1, min(budget, int(limit)))
    by_gap = max(1, int(duration // max(1.0, float(min_gap))))
    count = min(want, by_gap)
    step = duration / count
    return [int(round(step * (i + 0.5) * 1000)) for i in range(count)]


def pick_tier(stream_info: dict, duration_s: float, max_bytes: int) -> list[str]:
    """URLs of the best ≤480p tier whose estimated size still fits the download cap.

    A 12-minute 480p part is well over 32MiB, so the cap has to choose the tier
    instead of rejecting the download after the fact. Order is best-first.
    """
    tiers = stream_info.get("tiers") or []
    if not tiers:
        return stream_info.get("urls") or []
    budget = max(1.0, float(max_bytes)) * 0.9
    for tier in tiers:
        bandwidth = int(tier.get("bandwidth") or 0)
        if bandwidth <= 0 or float(duration_s or 0) <= 0:
            return tier.get("urls") or []
        if bandwidth * float(duration_s) / 8 <= budget:
            return tier.get("urls") or []
    return tiers[-1].get("urls") or []


def coverage(segments, duration: float) -> dict:
    """How much of the video the subtitles actually anchor, in seconds."""
    total = float(duration or 0) * 1000.0
    spans = sorted((int(s.get("from_ms", 0)), int(s.get("to_ms", 0)))
                   for s in (segments or []) if int(s.get("to_ms", 0)) > int(s.get("from_ms", 0)))
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 500:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    covered = sum(end - start for start, end in merged)
    holes = []
    cursor = 0
    for start, end in (merged if spans else []):
        if start - cursor >= 20000:
            holes.append({"from_ms": cursor, "to_ms": start})
        cursor = max(cursor, end)
    if spans and total and total - cursor >= 20000:
        holes.append({"from_ms": cursor, "to_ms": int(total)})
    return {
        "total_ms": int(total),
        "covered_ms": int(covered),
        "ratio": round(min(1.0, covered / total), 3) if total else 0.0,
        "holes": holes[:6],
        "segments": len(spans),
        "timed": bool(spans),
    }


def _state_of(subtitle: dict) -> tuple[str, str]:
    if subtitle.get("segments"):
        return "timed", ""
    if subtitle.get("chars"):
        return "text_only", subtitle.get("reason") or "no_timeline"
    return "none", subtitle.get("reason") or "unavailable"


async def open_reading(client, store, *, bvid: str, page: int, scope: str,
                       max_chars: int = 50000) -> dict:
    """Fetch one part's metadata and subtitle; store it as a new revision."""
    bvid = str(bvid or "").strip()
    if not BVID.match(bvid):
        raise ReadingError("bad_bvid", "BV 号格式不正确。")
    try:
        info = await client.detail(bvid)
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as a reason
        raise ReadingError("detail_failed", f"取视频信息失败：{exc}") from None
    parts = info.get("parts") or []
    page = max(1, int(page or 1))
    if parts and page > len(parts):
        raise ReadingError("bad_page", f"该视频只有 {len(parts)} 个分P。")
    part = parts[page - 1] if parts and len(parts) >= page else {
        "page": 1, "cid": info.get("cid"), "part_title": "", "duration": info.get("duration")}
    cid = int(part.get("cid") or info.get("cid") or 0)
    if cid <= 0:
        raise ReadingError("no_cid", "该视频没有可用的 cid。")
    subtitle = await client.subtitle(bvid, cid)
    state, reason = _state_of(subtitle)
    record = store.begin_reading(
        reading_id=new_id("rd"), bvid=bvid, page=page, cid=cid,
        title=str(info.get("title") or ""), author=str(info.get("owner") or ""),
        part_title=str(part.get("part_title") or ""),
        duration=part.get("duration") or info.get("duration"),
        fetched_at=clock_now(), source_type="bvid", subtitle_state=state,
        subtitle_reason=reason, evidence="text_read" if state != "none" else "metadata_only",
        scope=scope)
    segments = [dict(row, text=str(row.get("text", ""))[:MAX_SEGMENT_CHARS])
                for row in (subtitle.get("segments") or []) if str(row.get("text", "")).strip()]
    if not segments and subtitle.get("text"):
        segments = [{"from_ms": 0, "to_ms": 0, "text": subtitle["text"][:max_chars]}]
    stored = store.add_segments(record["reading_id"], record["revision"], segments,
                                lang=str(subtitle.get("lang") or ""))
    record["segments"] = stored
    record["coverage"] = coverage(segments, record["duration"])
    record["subtitle_reason"] = reason
    return record


def render_page(store, reading_id: str, revision: int, offset: int, *, scope: str) -> dict:
    """One page of timestamped lines plus a signed cursor for the next page."""
    record = store.reading(reading_id, scope)
    if not record or int(record["revision"]) != int(revision):
        raise ReadingError("not_found", "这次阅读记录不存在，或已被新的阅读取代。")
    rows = store.segments(reading_id, revision)
    total = len(rows)
    offset = max(0, min(int(offset or 0), max(0, total - 1)))
    items, used = [], 0
    for row in rows[offset:offset + PAGE_SEGMENTS]:
        text = row["text"]
        if items and used + len(text) > PAGE_CHARS:
            break
        used += len(text)
        items.append({"n": row["idx"] + 1, "t": _stamp(row["from_ms"]), "text": text})
    next_offset = offset + len(items)
    done = next_offset >= total
    return {
        "reading_id": reading_id, "revision": int(revision), "page": int(record["page"]),
        "title": record["title"], "part_title": record["part_title"], "author": record["author"],
        "duration": record["duration"], "subtitle_state": record["subtitle_state"],
        "subtitle_reason": record.get("subtitle_reason", ""),
        "items": items, "offset": offset, "total": total, "done": done,
        "cursor": None if done else store.cursor(reading_id, revision, next_offset, scope),
        "coverage": coverage(rows, record["duration"]),
    }


def _stamp(ms: int) -> str:
    seconds = max(0, int(ms) // 1000)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
