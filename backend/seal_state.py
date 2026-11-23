"""封批代唯一定源：五页共用的正式代解析。

二次封批纪要（甲路径）：
- 正式代 = seal_snapshots 中 status='已封批' 的代；
- 同批只允许存在一个正式代；
- 无正式代时回退批次当前代，保证未封批页面也有稳定展示。
"""
from __future__ import annotations

FORMAL_SEAL_STATUS = "已封批"


def formal_seal_rows(conn, batch_id: int) -> list:
    return conn.execute(
        """SELECT * FROM seal_snapshots
           WHERE batch_id=? AND status=?
           ORDER BY batch_generation, id""",
        (batch_id, FORMAL_SEAL_STATUS),
    ).fetchall()


def official_generation(conn, batch_id: int, fallback: int | None = None) -> int | None:
    rows = formal_seal_rows(conn, batch_id)
    if rows:
        return int(rows[0]["batch_generation"])
    return int(fallback) if fallback is not None else None
