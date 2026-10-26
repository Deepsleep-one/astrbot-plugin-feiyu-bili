"""Read-only bridge for the companion plugin (`BiliReadPortV1`, M6-A).

这个模块只做一件事：把**这个插件已经读到的东西**摊成一份只读快照交出去，
让陪伴插件能说「她今天看过什么、记得什么、还欠主人几次点头」，
**不替她做任何决定，也不代替主人点头**。

守住的边界（都是刻意的）：

- **只读**：不写 `browse.db`、不改额度、不改 `next_wake`、不触发 ASR、
  不下载、不联网、不读凭证文件；
- **同步**：陪伴端明确要求同步返回；要异步就另立端口版本，不在这里模糊掉；
- **不抛异常**：任何内部问题都变成「空快照 + note」，绝不让一条脏数据把聊天打断；
- **白名单由对端再筛一遍**：这里给的是「插件自己知道的字段」，
  陪伴端 `ports/bili.py` 还有一层白名单，两层都不放过的才会进上下文；
- **不冒充证据**：`metadata_only` 就是 `metadata_only`，
  「拿到过条目」不等于「读过」，抽帧不等于看完整片。

映射规则（保守方向）：

- `text_read` → `official_subtitle`（有官方/字幕文本）；`machine_transcript` /
  `asr_transcribed` / `asr_weak` → `machine_transcript`（确实转过写）；
  `frames_ready` / `images_submitted` / `visual_observed` → `frames`；
  其余（含 `no_speech_evidence` / `visual_unavailable`）→ `metadata_only`。
  **转写弱不弱、够不够判一个赞，仍然是本插件自己的闸门说了算**，
  这里只回答「证据是哪一类」。
- `coverage`：只有「字幕/转写类证据」且分段确实覆盖到片长的 90% 以上才算 `full`，
  否则 `partial`；没有分段就是 `none`。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

PORT_NAME = "BiliReadPortV1"
SNAPSHOT_VERSION = 1

TZ = timezone(timedelta(hours=8))

#: 插件内部证据串 → 陪伴端白名单（`ports.bili.EVIDENCE_ORDER`）。
EVIDENCE_MAP = {
    "text_read": "official_subtitle",
    "machine_transcript": "machine_transcript",
    "asr_transcribed": "machine_transcript",
    "asr_weak": "machine_transcript",
    "frames_ready": "frames",
    "images_submitted": "frames",
    "visual_observed": "frames",
}
DEFAULT_EVIDENCE = "metadata_only"

#: 只有这两类才算「有文本可引用」，用来判 coverage。
TEXT_EVIDENCE = ("official_subtitle", "machine_transcript")

SUBTITLE_MAP = {"official": "official", "asr": "asr", "none": "none"}
DEFAULT_SUBTITLE = "unknown"

#: 覆盖率达到这个比例才敢说「看完整」。
FULL_COVERAGE_RATIO = 0.9

#: 一次查询最多给多少条（防对端传个大数把这里拖死）。
LIMITS = {"readings": (1, 50), "reflections": (1, 20), "interests": (1, 30), "proposals": (1, 20)}


def evidence_of(raw) -> str:
    """插件内部证据串 → 白名单值。认不出的按 `metadata_only` 处理（宁少不多）。"""
    return EVIDENCE_MAP.get(str(raw or "").strip(), DEFAULT_EVIDENCE)


def subtitle_of(raw) -> str:
    return SUBTITLE_MAP.get(str(raw or "").strip(), DEFAULT_SUBTITLE)


def coverage_of(reading: dict, segments: list[dict]) -> str:
    """`full` / `partial` / `none`，只看「读到了哪一段」。"""
    if evidence_of(reading.get("evidence")) not in TEXT_EVIDENCE:
        return "none"
    if not segments:
        return "none"
    try:
        total_ms = float(reading.get("duration") or 0) * 1000.0
    except (TypeError, ValueError):
        total_ms = 0.0
    try:
        reached = max(int(seg.get("to_ms") or 0) for seg in segments)
    except (TypeError, ValueError):
        return "none"
    if total_ms <= 0:
        return "partial"  # 不知道片长就不敢说看完整
    return "full" if reached >= total_ms * FULL_COVERAGE_RATIO else "partial"


def _clamp(value, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value, limit: int = 200) -> str:
    out = str(value or "").strip()
    return out[:limit] if limit and len(out) > limit else out


def _local_day(moment: datetime | None = None) -> str:
    stamp = moment or datetime.now(TZ)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=TZ)
    return stamp.astimezone(TZ).strftime("%Y-%m-%d")


def _refs(raw) -> list[str]:
    """`refs_json` 可能是 dict、list 或空：只取能当指针用的字符串。"""
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        text = _text(item, 120)
        if text:
            out.append(text)
    return out[:8]


class BiliReadFacade:
    """绑在一个**活着的**插件实例上，只读地摊开它当前的状态。

    刻意不接受外部传进来的 store / 路径：能读什么完全由宿主实例决定，
    调用方（陪伴插件）只能收窄范围（`scope` / 各 `max_*`），不能换个库读。
    """

    def __init__(self, plugin) -> None:
        self.plugin = plugin

    # ---- 对外唯一的入口 ------------------------------------------------
    def snapshot(self, query: dict | None = None) -> dict:
        query = query if isinstance(query, dict) else {}
        scope = _text(query.get("scope"), 200)
        snap = {
            "version": SNAPSHOT_VERSION,
            "queried_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "scope": scope,
            "readings": [],
            "reflections": [],
            "interests": [],
            "proposals": [],
            "runtime": {},
            "notes": [],
        }
        notes = snap["notes"]
        if not scope:
            notes.append("查询没带 scope：不知道是哪个会话的阅读，一律不返回。")
            return snap

        plugin = self.plugin
        rstore = getattr(plugin, "rstore", None)
        if rstore is None:
            notes.append("阅读存储还没初始化（插件没启动完或总开关关着）。")
            return snap

        limits = {
            "readings": _clamp(query.get("max_readings"), *LIMITS["readings"], 10),
            "reflections": _clamp(query.get("max_reflections"), *LIMITS["reflections"], 4),
            "interests": _clamp(query.get("max_interests"), *LIMITS["interests"], 8),
            "proposals": _clamp(query.get("max_proposals"), *LIMITS["proposals"], 5),
        }

        snap["readings"] = self._readings(rstore, scope, limits["readings"], notes)
        snap["reflections"] = self._reflections(rstore, scope, limits["reflections"], notes)
        snap["interests"] = self._interests(rstore, scope, limits["interests"], notes)
        snap["proposals"] = self._proposals(rstore, scope, limits["proposals"], notes)
        snap["runtime"] = self._runtime(rstore, scope, notes)
        return snap

    # ---- 各段 ----------------------------------------------------------
    def _readings(self, rstore, scope: str, limit: int, notes: list[str]) -> list[dict]:
        try:
            rows = rstore.readings(scope, limit=limit)
        except Exception as exc:  # noqa: BLE001 - 只读诊断不该抛
            notes.append(f"读阅读记录失败：{type(exc).__name__}")
            return []
        out = []
        for row in rows or []:
            reading_id = _text(row.get("reading_id"), 80)
            bvid = _text(row.get("bvid"), 40)
            if not reading_id or not bvid:
                continue
            revision = max(1, _int(row.get("revision"), 1))
            try:
                segments = rstore.segments(reading_id, revision) or []
            except Exception:  # noqa: BLE001
                segments = []
            out.append({
                "reading_id": reading_id,
                "bvid": bvid,
                "page": max(1, _int(row.get("page"), 1)),
                "cid": str(row.get("cid") or ""),
                "revision": revision,
                "fetched_at": _text(row.get("fetched_at"), 40),
                "evidence": evidence_of(row.get("evidence")),
                "subtitle_state": subtitle_of(row.get("subtitle_state")),
                "coverage": coverage_of(row, segments),
                "scope": scope,
            })
        if not out:
            notes.append("这个会话还没有任何阅读记录。")
        return out

    def _reflections(self, rstore, scope: str, limit: int, notes: list[str]) -> list[dict]:
        try:
            rows = rstore.reflections(scope=scope, state="active", limit=limit)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"读认识失败：{type(exc).__name__}")
            return []
        out = []
        for row in rows or []:
            text = _text(row.get("new_view"), 300)
            if not text:
                continue
            journal_id = _text(row.get("journal_id"), 80)
            try:
                memory_id = _text(rstore.memory_link("bili_view", journal_id), 80) if journal_id else ""
            except Exception:  # noqa: BLE001
                memory_id = ""
            refs = _refs(row.get("refs_json")) or ([f"journal:{journal_id}"] if journal_id else [])
            out.append({
                "reflection_id": str(row.get("id") or ""),
                "journal_id": journal_id,
                "topic": _text(row.get("topic"), 60),
                "text": text,
                "state": _text(row.get("state"), 20) or "active",
                "supersedes": str(row.get("supersedes") or ""),
                "memory_id": memory_id,
                "source_refs": refs,
                "revision": 1,
                "scope": scope,
                "updated_at": _text(row.get("updated_at"), 40),
                "source": "reflection",
            })
        return out

    def _interests(self, rstore, scope: str, limit: int, notes: list[str]) -> list[dict]:
        try:
            rows = rstore.interests(scope, limit=limit)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"读兴趣失败：{type(exc).__name__}")
            return []
        out = []
        for row in rows or []:
            topic = _text(row.get("topic"), 60)
            if not topic:
                continue
            try:
                weight = float(row.get("weight") or 0.0)
            except (TypeError, ValueError):
                weight = 0.0
            out.append({
                "topic": topic,
                "weight": weight,
                "updated_at": _text(row.get("last_seen"), 40),
            })
        return out

    def _proposals(self, rstore, scope: str, limit: int, notes: list[str]) -> list[dict]:
        try:
            rows = rstore.proposals(scope, state="pending", limit=limit)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"读待批准提议失败：{type(exc).__name__}")
            return []
        out = []
        for row in rows or []:
            proposal_id = _text(row.get("proposal_id"), 80)
            if not proposal_id:
                # 没有稳定 ID 就不给：宁可少一条，也不能让对端按序号去批准
                notes.append("有一条待批准提议缺稳定 proposal_id，已跳过。")
                continue
            out.append({
                "proposal_id": proposal_id,
                "bvid": _text(row.get("bvid"), 40),
                "page": max(1, _int(row.get("page"), 1)),
                "state": _text(row.get("state"), 20) or "pending",
                "revision": 1,
                "created_at": _text(row.get("created_at"), 40),
                "scope": scope,
            })
        return out

    def _runtime(self, rstore, scope: str, notes: list[str]) -> dict:
        plugin = self.plugin
        config = getattr(plugin, "config", {}) or {}
        kv = getattr(plugin, "store", None)

        def kv_get(key, default=None):
            try:
                return kv.get(key, default) if kv is not None else default
            except Exception:  # noqa: BLE001
                return default

        def kv_int(method: str) -> int:
            try:
                return int(getattr(kv, method)())
            except Exception:  # noqa: BLE001
                return 0

        try:
            likes_enabled = bool(plugin._likes_enabled())
        except Exception:  # noqa: BLE001
            likes_enabled = False

        try:
            asr_used = int(rstore.asr_used(scope, _local_day()))
        except Exception:  # noqa: BLE001
            asr_used = 0

        last_error_type = ""
        raw_error = kv_get("last_error")
        if raw_error:
            try:
                last_error_type = _text(json.loads(raw_error).get("type"), 60)
            except (TypeError, ValueError, AttributeError):
                last_error_type = ""

        return {
            "auto": bool(kv_get("auto", False)),
            "likes_enabled": likes_enabled,
            "next_wake": _text(kv_get("next_wake"), 40),
            "round": kv_int("round_count"),
            "likes_used": kv_int("daily_likes"),
            "likes_limit": max(0, _int(config.get("daily_likes"), 3)),
            "shares_used": kv_int("daily_count"),
            "shares_limit": max(0, _int(config.get("daily_shares"), 5)),
            "asr_used": max(0, asr_used),
            "asr_limit": max(0, _int(config.get("asr_daily_limit"), 5)),
            "share_cooldown_minutes": max(0, _int(config.get("share_cooldown_minutes"), 120)),
            "quiet_start": max(0, _int(config.get("quiet_start"), 23)),
            "quiet_end": max(0, _int(config.get("quiet_end"), 9)),
            "last_error_type": last_error_type,
        }
