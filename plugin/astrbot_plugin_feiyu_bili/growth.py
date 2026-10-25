"""Growth: what she actually takes away from her own reading.

Three deliberately small pieces, all of them owner-supervised:

- interests: a weighted list of what she keeps coming back to. It colours how she talks
  about her day; it is never presented as a fact about the owner.
- proposals: videos she would like to read next. They are only suggestions — nothing is
  fetched or read until the owner approves one.
- views: her current take on a topic, written into the long-term store as ``derived``
  (never ``owner_asserted``), with the reading id as evidence and a tombstone when withdrawn.

Nothing here touches cookies, signed CDN URLs or private chat text.
"""
from __future__ import annotations

import re

try:
    from .reading import new_id
except ImportError:  # loaded standalone by the offline tests
    from reading import new_id

KIND_VIEW = "bili_view"
LIST_LIMIT = 5
_FILLER = re.compile(r"[\s，。、；：！？,.;:!?（）()\[\]【】《》\"'“”‘’·—…~]+")
_FILLER_WORDS = ("b站", "视频", "一个视频", "这一p", "这p", "合集")


def _is_filler(text) -> bool:
    """'B站视频' / '这一P' are placeholders, not topics — nothing to be interested in."""
    key = topic_key(text)
    for word in _FILLER_WORDS:
        key = key.replace(word, "")
    return not key


def clean_topic(text, limit: int = 24) -> str:
    """A topic as the owner would recognise it: trimmed, single line, no control chars."""
    if not isinstance(text, str):
        return ""
    value = _FILLER.sub(" ", text).strip()
    value = re.sub(r"[\x00-\x1f\x7f]", "", value)
    return value[:limit].strip()


def topic_key(text) -> str:
    """Comparison key for the same topic said twice (case/space/punctuation insensitive)."""
    value = clean_topic(text, limit=64).lower()
    return _FILLER.sub("", value)


def topic_from(reading: dict, page: dict | None = None) -> str:
    """Prefer the part title (that is what she actually read), fall back to the video title."""
    page = page or {}
    for candidate in (page.get("part_title"), reading.get("part_title"),
                      reading.get("title"), page.get("title")):
        topic = clean_topic(candidate)
        if not _is_filler(topic):
            return topic
    return ""


def interest_line(store, scope: str, limit: int = 3) -> str:
    """One line for the prompt: what she has been paying attention to lately."""
    rows = store.interests(scope, limit=max(1, int(limit)))
    if not rows:
        return ""
    parts = [f"{r['topic']}（{_times(r['weight'])}）" for r in rows]
    return "我最近比较在意：" + "、".join(parts)


def _times(weight) -> str:
    try:
        value = float(weight)
    except (TypeError, ValueError):
        return "看过"
    if value >= 4:
        return "反复看"
    if value >= 2:
        return "看过多条"
    return "看过一次"


async def propose(client, store, scope: str, terms, *, limit: int = 3, per_term: int = 2,
                  skips=None) -> list[dict]:
    """Turn her interests into a short, honest list of candidates. No fetching, no reading."""
    limit = max(1, min(5, int(limit)))
    skipped = {b for b in (skips or [])}
    skipped.update(store.read_bvids(scope, limit=80))
    skipped.update(row["bvid"] for row in store.proposals(scope, state="pending", limit=50))
    created: list[dict] = []
    for term in _unique_terms(terms):
        if len(created) >= limit:
            break
        try:
            hits = await client.search(term, per_term)
        except Exception:
            continue
        for hit in hits:
            if len(created) >= limit:
                break
            bvid = str(hit.get("bvid") or "")
            if not bvid or bvid in skipped:
                continue
            skipped.add(bvid)
            row = store.add_proposal(
                new_id("prop"), scope, bvid, page=1,
                title=clean_topic(hit.get("title"), limit=60),
                author=clean_topic(hit.get("author"), limit=30),
                duration=None,
                reason=f"来自我最近在意的「{term}」",
                source="interest_search")
            created.append(row)
    return created


def _unique_terms(terms) -> list[str]:
    seen, result = set(), []
    for term in terms or []:
        topic = clean_topic(term)
        key = topic_key(topic)
        if _is_filler(topic) or key in seen:
            continue
        seen.add(key)
        result.append(topic)
    return result


def format_proposals(rows) -> str:
    """Numbered exactly like ``store.proposal_at`` resolves them: pending, oldest first."""
    if not rows:
        return "这会儿没有想读的，你给个 BV 或者主题吧。"
    lines = ["我想读这几条（还没读，等你点头）："]
    for index, row in enumerate(rows, start=1):
        meta = row.get("author") or ""
        if meta:
            meta = f" —— {meta}"
        minutes = _minutes(row.get("duration"))
        tail = f"（{minutes}）" if minutes else ""
        lines.append(f"{index}. {row.get('title') or row.get('bvid')}{meta}{tail}")
        if row.get("reason"):
            lines.append(f"   理由：{row['reason']}")
    lines.append("回「/刷站 读 编号」我就去读，回「/刷站 不读 编号」就换掉。")
    return "\n".join(lines)


def _minutes(duration) -> str:
    try:
        seconds = float(duration)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return f"约 {max(1, round(seconds / 60))} 分钟"


def view_payload(*, owner_id: str, journal: dict, reflection: dict | None, reading: dict,
                 page: dict | None = None) -> dict | None:
    """What goes into the long-term store when the owner keeps a journal.

    Only the owner-approved journal becomes a view; a journal that was never kept is not
    a view at all. ``kind='fact'`` + ``epistemic_type='derived'`` keeps it out of the
    owner's own assertions and marks it 〔派生整理〕 in every future injection.
    """
    fields = journal.get("fields") or {}
    view = clean_topic((reflection or {}).get("new_view") or fields.get("revision") or "", limit=200)
    if not view:
        return None
    topic = topic_from(reading, page) or clean_topic(fields.get("encounter"), limit=24) or "B站阅读"
    title = clean_topic(reading.get("title"), limit=60)
    part = clean_topic((page or {}).get("part_title") or reading.get("part_title"), limit=40)
    where = f"《{title}》" + (f"（{part}）" if part and part != title else "")
    content = f"我自己整理的认识（不是主人的说法）：读{where}之后，{view}"
    return {
        "owner_id": owner_id,
        "kind": "fact",
        "content": content[:1900],
        "epistemic_type": "derived",
        "scope_type": "owner_private",
        "fact_key": f"feiyu.bili.view:{topic_key(topic)}",
        "importance": 2,
        "evidence": [{
            "kind": "bilibili_reading",
            "ref": f"{reading.get('reading_id')}@r{reading.get('revision')}",
            "excerpt": view[:300],
        }],
        "created_by": "astrbot_plugin_feiyu_bili",
        "op_key": f"bili-view:{journal.get('journal_id')}",
        "supersede_epistemic": ("derived",),
    }


def write_view(db, store, payload: dict, bili_id: str) -> str:
    """Write one derived view; returns the memory record id ('' when the store is absent)."""
    if db is None or not payload:
        return ""
    record = db.add_record(**payload)
    memory_id = str((record or {}).get("id") or "")
    if memory_id:
        store.link_memory(KIND_VIEW, bili_id, memory_id)
    return memory_id


def forget_view(db, store, bili_id: str, reason: str = "主人撤回了这条认识") -> bool:
    """Withdrawing a reflection also forgets the derived view it produced."""
    memory_id = store.memory_link(KIND_VIEW, bili_id)
    if not memory_id:
        return False
    ok = bool(db.forget_record(memory_id, reason=reason,
                               actor="astrbot_plugin_feiyu_bili")) if db else False
    store.drop_memory_link(KIND_VIEW, bili_id)
    return ok
