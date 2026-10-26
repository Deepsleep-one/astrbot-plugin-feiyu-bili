"""B站侧的分享交接面（M6-D）：只写 outbox，不发送。

`bili_read_port()` 是「你看她看过什么」，这个 `bili_share_port()` 是「把要发的
一条交出去」。两者都同步、都离线、都不碰网络、都不调用这里自己的发送函数。

为什么不在这个类里发送？因为 M6-D 的全部意义就是**发送权只有一个**。B站这边
把活写进 outbox，companion 认领、发送、回执；B站只认回执，不自己动手。这样
「同一条分享了两次」和「分享了但没人知道」都不会再发生。

`submit()` / `ack()` 会写这个插件自己的库（`share_outbox` 表），这是它的本职：
它管的是「我要发什么」和「发的结果是什么」。它不写记忆库、不碰 `deliveries`。
"""

from __future__ import annotations

try:  # 包内正常导入
    from .core import BVID
    from .share_outbox import (
        OUTBOX_VERSION,
        OUTCOMES,
        ShareConflict,
        ShareOutbox,
        ShareOutboxError,
        payload_hash,
    )
except ImportError:  # 离线测试直接按文件加载，没有包上下文
    import importlib.util
    from pathlib import Path

    def _load_sibling(name, filename):
        import sys

        existing = sys.modules.get(name)
        if existing is not None:
            return existing
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    _core = _load_sibling("_feiyu_core_for_share_port", "core.py")
    _outbox = _load_sibling("_feiyu_outbox_for_share_port", "share_outbox.py")
    BVID = _core.BVID
    OUTBOX_VERSION = _outbox.OUTBOX_VERSION
    OUTCOMES = _outbox.OUTCOMES
    ShareConflict = _outbox.ShareConflict
    ShareOutbox = _outbox.ShareOutbox
    ShareOutboxError = _outbox.ShareOutboxError
    payload_hash = _outbox.payload_hash

SHARE_PORT_NAME = "BiliSharePortV1"
SHARE_PORT_VERSION = 1

PLAN_FIELDS = (
    "generation",
    "scope",
    "bvid",
    "page",
    "revision",
    "target",
    "message",
    "grant_revision",
    "deadline",
)
ACK_FIELDS = ("generation", "outcome", "detail")

_QUERY_LIMITS = {"limit": (1, 20), "ttl_minutes": (1, 120)}


def _clamp(value, low, high, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


class BiliSharePort:
    """交接面：`submit` 落 outbox，`claim` 交给发送端，`ack` 收结果。"""

    version = SHARE_PORT_VERSION
    port = SHARE_PORT_NAME

    def __init__(self, plugin, *, outbox: ShareOutbox | None = None) -> None:
        store = getattr(plugin, "store", None)
        db = getattr(store, "db", None)
        if outbox is None:
            if db is None:
                raise ShareOutboxError("插件还没开库：交接面不能凭空建库")
            outbox = ShareOutbox(db)
        self.plugin = plugin
        self.outbox = outbox

    # ------------------------------------------------------------------ info
    def status(self) -> dict:
        return {
            "port": SHARE_PORT_NAME,
            "version": SHARE_PORT_VERSION,
            "outbox_version": OUTBOX_VERSION,
            "counts": self.outbox.counts(),
            "sends_here": False,
        }

    # ----------------------------------------------------------------- write
    def submit(self, plan: dict | None = None) -> dict:
        """把一条「该发这个」写进 outbox。重复提交同一条返回 duplicate。"""
        plan = dict(plan or {})
        missing = [name for name in ("generation", "scope", "bvid", "target", "message")
                   if not str(plan.get(name) or "").strip()]
        if missing:
            raise ShareOutboxError(f"交接计划缺字段：{'、'.join(missing)}")
        return self.outbox.enqueue(
            generation=str(plan.get("generation")),
            scope=str(plan.get("scope")),
            bvid=str(plan.get("bvid")),
            target=str(plan.get("target")),
            message=str(plan.get("message")),
            page=plan.get("page", 1),
            revision=plan.get("revision", 0),
            grant_revision=str(plan.get("grant_revision") or ""),
            deadline=str(plan.get("deadline") or ""),
        )

    # ------------------------------------------------------------------ read
    def claim(self, query: dict | None = None) -> list[dict]:
        """认领一批待发。谁先认领谁负责发，别的人拿不到。"""
        query = dict(query or {})
        return self.outbox.claim(
            str(query.get("scope") or ""),
            limit=_clamp(query.get("limit"), 1, 20, 1),
            token=str(query.get("token") or ""),
            ttl_minutes=_clamp(query.get("ttl_minutes"), 1, 120, 15),
        )

    def pending(self, query: dict | None = None) -> list[dict]:
        query = dict(query or {})
        return self.outbox.pending(
            str(query.get("scope") or ""),
            limit=_clamp(query.get("limit"), 1, 20, 20),
        )

    # -------------------------------------------------------------- ack back
    def ack(self, result: dict | None = None) -> dict:
        """收发送端的回执。sent/unknown/failed/refused 都认，别的名字直接拒。"""
        result = dict(result or {})
        generation = str(result.get("generation") or "").strip()
        outcome = str(result.get("outcome") or "").strip()
        if not generation:
            raise ShareOutboxError("回执缺 generation")
        if outcome not in OUTCOMES:
            raise ShareOutboxError(f"回执里的结果名不认识：{outcome}")
        return self.outbox.ack(
            generation, outcome, detail=str(result.get("detail") or "")
        )


def plan_of(*, generation: str, scope: str, bvid: str, target: str, message: str,
            page: int = 1, revision: int = 0, grant_revision: str = "",
            deadline: str = "") -> dict:
    """造一份交接计划，顺手把载荷指纹算出来给人核对。"""
    if not BVID.fullmatch(str(bvid or "")):
        raise ShareOutboxError(f"BV 号不合法：{bvid}")
    return {
        "generation": generation,
        "scope": scope,
        "bvid": bvid,
        "page": int(page or 1),
        "revision": int(revision or 0),
        "target": target,
        "message": message,
        "grant_revision": grant_revision,
        "deadline": deadline,
        "payload_hash": payload_hash(
            scope=scope, bvid=bvid, page=int(page or 1),
            revision=int(revision or 0), target=target, message=message,
        ),
    }


__all__ = [
    "SHARE_PORT_NAME",
    "SHARE_PORT_VERSION",
    "BiliSharePort",
    "ShareConflict",
    "ShareOutboxError",
    "plan_of",
]
