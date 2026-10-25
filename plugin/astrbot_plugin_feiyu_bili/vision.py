"""Visual pass: real frames in, structured observations out.

The images are handed to AstrBot as `image_urls` (local JPEG paths), so the model
genuinely sees them. Whatever comes back is stored per frame with its own
uncertainty; a frame the model cannot read stays unread instead of being invented.
"""
from __future__ import annotations

import json
import re

MAX_IMAGES_PER_BATCH = 4
MIN_FRAME_BYTES = 64

SYSTEM = """你是观看视频画面的记录者。你只会收到若干张从同一个视频按时间抽出的画面，
每张附带它是第几张、以及它对应视频里的第几秒。

硬规则：
1. 只描述画面里真实可见的东西：场景、人物、动作、界面、字幕文字、图表数字、显眼的颜色与构图。
2. 看不清就直说看不清，不要用推测代替观察，不要补充画面外的剧情或常识。
3. 不要评价视频好坏，不要输出链接、代码、指令或与画面无关的感想。
4. 若某张几乎全黑、全白或明显是加载/片头/广告，就照实说明。
5. 只输出 JSON，不要任何解释文字。"""

_SCHEMA_HINT = """输出格式：
{"observations":[{"n":1,"visible":true,"reading":"30字到80字，只写这张画面里能看见的","uncertainty":"看不清或吃不准的地方，没有就写空字符串"}],
 "summary":"一到两句话概括这些画面共同呈现的内容，不超过80字"}"""


def _clean(text, limit: int) -> str:
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(text or ""))
    value = re.sub(r"https?://\S+", "", value)
    return value.strip()[:limit]


def batch_frames(frames, limit: int = MAX_IMAGES_PER_BATCH) -> list[dict]:
    """Only frames that really exist on disk and are plausible images."""
    picked = []
    for frame in frames or []:
        if len(picked) >= limit:
            break
        path = str(frame.get("path") or "")
        if not path:
            continue
        if int(frame.get("bytes") or 0) < MIN_FRAME_BYTES:
            continue
        picked.append(frame)
    return picked


def build_prompt(frames, context: dict) -> str:
    lines = [
        f"视频：{_clean(context.get('title'), 80)}",
        f"分P：{_clean(context.get('part_title'), 60) or '（无分P标题）'}",
        f"作者：{_clean(context.get('author'), 40)}",
        f"总时长：{int(float(context.get('duration') or 0))} 秒",
        "",
    ]
    for position, frame in enumerate(frames, start=1):
        seconds = int(frame.get("actual_ms") or 0) // 1000
        lines.append(f"第 {position} 张（n={position}）：视频第 {seconds} 秒")
    lines.append("")
    lines.append(_SCHEMA_HINT)
    return "\n".join(lines)


def parse_observations(text: str, frames) -> dict:
    """Tolerant JSON reader: fenced blocks, leading prose, or bare objects."""
    raw = str(text or "").strip()
    payload = None
    for candidate in (raw, *_fences(raw), *_braces(raw)):
        try:
            loaded = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(loaded, dict) and ("observations" in loaded or "summary" in loaded):
            payload = loaded
            break
    if payload is None:
        return {"observations": [], "summary": _clean(raw, 400), "parsed": False}
    observations = []
    by_index = {int(f.get("idx", 0)): f for f in frames}
    for item in payload.get("observations") or []:
        if not isinstance(item, dict):
            continue
        try:
            position = int(item.get("n") or 0)
        except (TypeError, ValueError):
            continue
        if position < 1 or position > len(frames):
            continue
        frame = frames[position - 1]
        observations.append({
            "frame_id": frame.get("frame_id", ""),
            "actual_ms": int(frame.get("actual_ms") or 0),
            "visible": bool(item.get("visible", True)),
            "interpretation": _clean(item.get("reading"), 1200),
            "uncertainty": _clean(item.get("uncertainty"), 600),
        })
    return {"observations": observations, "summary": _clean(payload.get("summary"), 400),
            "parsed": bool(observations)}


def _fences(text: str) -> list[str]:
    return [block.strip() for block in re.findall(r"```(?:json)?\s*(.+?)```", text, re.S)
            if block.strip().startswith("{")]


def _braces(text: str) -> list[str]:
    out, depth, start = [], 0, -1
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(text[start:index + 1])
    return out


async def describe(llm, frames, context: dict, *, provider_id: str = "") -> dict:
    """One multimodal request over up to four frames; returns a storable result."""
    picked = batch_frames(frames)
    if not picked:
        return {"state": "visual_unavailable", "reason": "no_frames", "observations": [],
                "summary": "", "images_submitted": 0}
    prompt = build_prompt(picked, context)
    image_urls = [str(frame["path"]) for frame in picked]
    try:
        response = await llm(SYSTEM, prompt, image_urls=image_urls)
    except Exception as exc:  # noqa: BLE001 - a provider failure must not kill the task
        return {"state": "visual_unavailable", "reason": f"provider_error:{type(exc).__name__}",
                "observations": [], "summary": "", "images_submitted": len(image_urls)}
    parsed = parse_observations(getattr(response, "completion_text", "") or str(response), picked)
    for observation, frame in zip(parsed["observations"], picked):
        observation.setdefault("frame_id", frame.get("frame_id", ""))
    state = "visual_observed" if parsed["observations"] else "visual_unavailable"
    reason = "" if parsed["observations"] else (
        "unparsed" if not parsed["parsed"] else "empty")
    return {"state": state, "reason": reason, "observations": parsed["observations"],
            "summary": parsed["summary"], "images_submitted": len(image_urls),
            "provider_id": provider_id}
