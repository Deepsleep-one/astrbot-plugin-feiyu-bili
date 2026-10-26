"""分享 outbox（M6-D）：把「要不要发这一条」和「真的发出去」拆成两件事。

M6-A 只读桥解决的是「她以前看过什么」，M6-C 的影子里解决的是「这一轮该不该发」，
但这个插件仍然自己握着发送。M6-D 要做的交接是：

    B站（出主意、管额度的原库事务）  ->  写 outbox  ->  companion（唯一发送端）
                                        <- ACK / unknown <-

所以这个模块只做三件事，一件也不多做：

1. `enqueue()`：在**原库事务内**写一条待发记录，带 `generation`（对账号）、
   `payload_hash`（同号不同载荷要能发现）、`grant_revision`（当时的授权版本）、
   `deadline`（过期即作废）；
2. `claim()`：把 pending 原子地改成 claimed，并把认领令牌写回去——两个进程同时
   认领，只有一个能拿到；
3. `ack()`：把结果落成 `acked`（确认发过）或 `unknown`（交出去了但不知道对方收没
   收到）。

`unknown` 是这里唯一真正重要的状态：它**占额度、不重放**。发一半崩了的分享如果
被当成「没发过」，主人那边就会收到重复的视频；当成「发过了」又会漏发。所以它单独
一档，谁也别猜。

这个模块不联网、不调模型、不调用发送函数，也不碰 `deliveries` 表——那一张表还是
老路径的账本，交接没完成之前两边各自记各自的账。
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import timedelta

try:  # 包内正常导入
    from .core import BVID, now
except ImportError:  # 离线测试直接按文件加载，没有包上下文
    import importlib.util
    from pathlib import Path

    _spec = importlib.util.spec_from_file_location(
        "_feiyu_core_for_outbox", Path(__file__).with_name("core.py")
    )
    _core = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_core)
    BVID, now = _core.BVID, _core.now

OUTBOX_VERSION = 1

STATE_PENDING = "pending"
STATE_CLAIMED = "claimed"
STATE_ACKED = "acked"
STATE_UNKNOWN = "unknown"
STATE_EXPIRED = "expired"
STATES = (
    STATE_PENDING,
    STATE_CLAIMED,
    STATE_ACKED,
    STATE_UNKNOWN,
    STATE_EXPIRED,
)

OUTCOME_SENT = "sent"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_FAILED = "failed"
OUTCOME_REFUSED = "refused"
OUTCOMES = (OUTCOME_SENT, OUTCOME_UNKNOWN, OUTCOME_FAILED, OUTCOME_REFUSED)

DEFAULT_TTL_MINUTES = 15
DEFAULT_DEADLINE_MINUTES = 30

FIELDS = (
    "generation",
    "scope",
    "bvid",
    "page",
    "revision",
    "target",
    "message",
    "payload_hash",
    "grant_revision",
    "deadline",
    "state",
    "attempts",
    "claim_token",
    "claimed_at",
    "outcome",
    "detail",
    "created_at",
    "updated_at",
)


class ShareConflict(RuntimeError):
    """同一个 generation 被塞了不同的载荷：宁可报错，不许覆盖。"""


class ShareOutboxError(ValueError):
    """入参不合格（缺字段、BV 号不合法、状态名不认识）。"""


def payload_hash(*, scope: str, bvid: str, page: int, revision: int,
                 target: str, message: str) -> str:
    """同一份载荷永远算出同一个指纹；任何一个字段变了就不再是同一件事。"""
    raw = "\n".join([
        str(scope or ""),
        str(bvid or ""),
        str(int(page or 1)),
        str(int(revision or 0)),
        str(target or ""),
        str(message or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _iso(moment) -> str:
    return (moment or now()).isoformat(timespec="seconds")


def _row(row) -> dict:
    return {key: row[key] for key in FIELDS}


class ShareOutbox:
    """挂在 B站原有连接上的分享 outbox。不新建库、不新建连接。"""

    def __init__(self, db: sqlite3.Connection, *, clock=None) -> None:
        if db is None:
            raise ShareOutboxError("outbox 需要一个已打开的连接：不在只读端新建库")
        self.db = db
        self.clock = clock
        self.ensure()

    # ---------------------------------------------------------------- schema
    def ensure(self) -> None:
        with self.db:
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS share_outbox(
                  generation TEXT PRIMARY KEY,
                  scope TEXT NOT NULL,
                  bvid TEXT NOT NULL,
                  page INTEGER NOT NULL DEFAULT 1,
                  revision INTEGER NOT NULL DEFAULT 0,
                  target TEXT NOT NULL DEFAULT '',
                  message TEXT NOT NULL DEFAULT '',
                  payload_hash TEXT NOT NULL,
                  grant_revision TEXT NOT NULL DEFAULT '',
                  deadline TEXT NOT NULL DEFAULT '',
                  state TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  claim_token TEXT NOT NULL DEFAULT '',
                  claimed_at TEXT NOT NULL DEFAULT '',
                  outcome TEXT NOT NULL DEFAULT '',
                  detail TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_share_outbox_state "
                "ON share_outbox(state, created_at)"
            )

    # --------------------------------------------------------------- writing
    def enqueue(self, *, generation: str, scope: str, bvid: str, target: str,
                message: str, page: int = 1, revision: int = 0,
                grant_revision: str = "", deadline: str = "",
                now=None) -> dict:
        generation = (generation or "").strip()
        scope = (scope or "").strip()
        bvid = (bvid or "").strip()
        target = (target or "").strip()
        message = (message or "").strip()
        if not generation:
            raise ShareOutboxError("缺 generation：没有对账号就没法去重")
        if not scope:
            raise ShareOutboxError("缺 scope：不知道这是发给哪个会话的")
        if not BVID.fullmatch(bvid):
            raise ShareOutboxError(f"BV 号不合法：{bvid}")
        if not target:
            raise ShareOutboxError("缺 target：不知道该发给谁")
        if not message:
            raise ShareOutboxError("正文是空的：不发一条空白分享")
        digest = payload_hash(
            scope=scope, bvid=bvid, page=page, revision=revision,
            target=target, message=message,
        )
        stamp = _iso(now)
        expiry = (deadline or "").strip()
        if not expiry:
            expiry = ((now or now_dt()) + timedelta(minutes=DEFAULT_DEADLINE_MINUTES)) \
                .isoformat(timespec="seconds")
        existing = self.get(generation)
        if existing is not None:
            if existing["payload_hash"] != digest:
                raise ShareConflict(
                    f"对账号 {generation} 已经记过另一份载荷了：不覆盖、不改写，"
                    "要么换一个 generation，要么先核对是谁发的"
                )
            return {**existing, "duplicate": True}
        with self.db:
            self.db.execute(
                "INSERT INTO share_outbox(generation,scope,bvid,page,revision,target,"
                "message,payload_hash,grant_revision,deadline,state,attempts,"
                "claim_token,claimed_at,outcome,detail,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    generation, scope, bvid, int(page or 1), int(revision or 0),
                    target, message, digest, (grant_revision or "").strip(), expiry,
                    STATE_PENDING, 0, "", "", "", "", stamp, stamp,
                ),
            )
        return {**self.get(generation), "duplicate": False}

    # --------------------------------------------------------------- reading
    def get(self, generation: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM share_outbox WHERE generation=?", (generation,)
        ).fetchone()
        return _row(row) if row else None

    def pending(self, scope: str = "", *, limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM share_outbox WHERE state=?"
        args: list = [STATE_PENDING]
        if (scope or "").strip():
            sql += " AND scope=?"
            args.append(scope.strip())
        sql += " ORDER BY created_at ASC, generation ASC LIMIT ?"
        args.append(max(1, int(limit)))
        return [_row(r) for r in self.db.execute(sql, args)]

    def rows(self, state: str = "", *, scope: str = "", limit: int = 50) -> list[dict]:
        if (state or "").strip() and state not in STATES:
            raise ShareOutboxError(f"状态名不认识：{state}")
        sql = "SELECT * FROM share_outbox WHERE 1=1"
        args: list = []
        if (state or "").strip():
            sql += " AND state=?"
            args.append(state)
        if (scope or "").strip():
            sql += " AND scope=?"
            args.append(scope.strip())
        sql += " ORDER BY created_at DESC, generation DESC LIMIT ?"
        args.append(max(1, int(limit)))
        return [_row(r) for r in self.db.execute(sql, args)]

    def counts(self) -> dict:
        out = {state: 0 for state in STATES}
        for row in self.db.execute(
            "SELECT state, count(*) FROM share_outbox GROUP BY state"
        ):
            if row[0] in out:
                out[row[0]] = int(row[1])
        out["total"] = sum(out[state] for state in STATES)
        return out

    # ----------------------------------------------------------- claim / ack
    def claim(self, scope: str = "", *, limit: int = 1, token: str = "",
              ttl_minutes: int = DEFAULT_TTL_MINUTES, now=None) -> list[dict]:
        """把 pending 原子地改成 claimed。

        先顺手把过期的 pending 标掉（过期的分享不该在半夜突然冒出来），
        再挑最早的几条认领。认领是「状态从 pending 变成 claimed 成功」这一个
        动作，所以两个进程同时来抢同一条时只有一条会被改到。
        """
        stamp = _iso(now)
        moment = now or now_dt()
        token = (token or "").strip() or f"claim:{stamp}"
        ttl = max(1, int(ttl_minutes))
        ttl_cut = (moment - timedelta(minutes=ttl)).isoformat(timespec="seconds")
        with self.db:
            self.db.execute(
                "UPDATE share_outbox SET state=?, updated_at=?, detail=? "
                "WHERE state=? AND deadline<>'' AND deadline<?",
                (STATE_EXPIRED, stamp, "过了 deadline，作废", STATE_PENDING, stamp),
            )
            self.db.execute(
                "UPDATE share_outbox SET state=?, updated_at=?, detail=? "
                "WHERE state=? AND claimed_at<>'' AND claimed_at<?",
                (STATE_PENDING, stamp, "认领超时，放回去等下一个人", STATE_CLAIMED, ttl_cut),
            )
            sql = (
                "SELECT generation FROM share_outbox WHERE state=? "
                "AND (deadline='' OR deadline>=?)"
            )
            args: list = [STATE_PENDING, stamp]
            if (scope or "").strip():
                sql += " AND scope=?"
                args.append(scope.strip())
            sql += " ORDER BY created_at ASC, generation ASC LIMIT ?"
            args.append(max(1, int(limit)))
            picked = [r[0] for r in self.db.execute(sql, args)]
            out = []
            for generation in picked:
                cur = self.db.execute(
                    "UPDATE share_outbox SET state=?, claim_token=?, claimed_at=?, "
                    "attempts=attempts+1, updated_at=? "
                    "WHERE generation=? AND state=?",
                    (STATE_CLAIMED, token, stamp, stamp, generation, STATE_PENDING),
                )
                if cur.rowcount:
                    out.append(self.get(generation))
        return out

    def ack(self, generation: str, outcome: str, *, detail: str = "",
            now=None) -> dict:
        """一次结果回执。发过了落 acked，说不清落 unknown，被拒也落 acked。"""
        generation = (generation or "").strip()
        outcome = (outcome or "").strip()
        if outcome not in OUTCOMES:
            raise ShareOutboxError(f"结果名不认识：{outcome}")
        row = self.get(generation)
        if row is None:
            raise ShareOutboxError(f"没有这条对账号：{generation}")
        if row["state"] in (STATE_ACKED, STATE_UNKNOWN):
            # 重复回执：第一次说了算，第二次只记账不改状态。
            return {**row, "duplicate": True}
        stamp = _iso(now)
        target_state = STATE_UNKNOWN if outcome == OUTCOME_UNKNOWN else STATE_ACKED
        with self.db:
            self.db.execute(
                "UPDATE share_outbox SET state=?, outcome=?, detail=?, updated_at=? "
                "WHERE generation=?",
                (target_state, outcome, str(detail or "")[:500], stamp, generation),
            )
        return {**self.get(generation), "duplicate": False}

    def expire(self, *, now=None, scope: str = "") -> int:
        stamp = _iso(now)
        sql = "UPDATE share_outbox SET state=?, updated_at=? WHERE state=? AND deadline<>'' AND deadline<?"
        args: list = [STATE_EXPIRED, stamp, STATE_PENDING, stamp]
        if (scope or "").strip():
            sql += " AND scope=?"
            args.append(scope.strip())
        with self.db:
            return int(self.db.execute(sql, args).rowcount or 0)


def now_dt():
    return now()
