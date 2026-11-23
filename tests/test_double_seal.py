"""同批二次封批禁令（甲路径）回归：三红先行。

约定（二次封批纪要·甲路径）：
- 同批只要已存在正式封批代，二次封批正式代零增并拒写；
- 不得产生影子代草案与正式代并列；
- 两人双窗同批抢再封须抢批次锁，后到只记争用痕，不写业务。

本文件三个失败测覆盖：
  test_second_seal_rejects_and_zero_formal_growth   甲：二次仍增正式代
  test_no_unpromoted_shadow_generation_coexists     乙：未晋升却并列
  test_concurrent_reseal_lock_contention_trace      争用：双人同批抢再封
外加五页同源校验。
"""
import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db

SEED_BATCH_CODE = "TB-CK-HD-01-001"  # 种子：已入库、校准有效、无开放游标
FORMAL = "已封批"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    # 夹具在文件库跑：tmp_path 下真实 SQLite 文件，非 :memory:
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _seed_batch(c: TestClient, h: dict) -> dict:
    items = c.get("/api/batches", headers=h).json()["items"]
    return next(x for x in items if x["batch_code"] == SEED_BATCH_CODE)


def _formal_seals(batch_id: int) -> list[dict]:
    with connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                """SELECT * FROM seal_snapshots
                   WHERE batch_id=? AND status=? ORDER BY batch_generation""",
                (batch_id, FORMAL),
            ).fetchall()
        ]


def _all_seals(batch_id: int) -> list[dict]:
    with connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM seal_snapshots WHERE batch_id=? ORDER BY id",
                (batch_id,),
            ).fetchall()
        ]


def _batch_row(batch_id: int) -> dict:
    with connect() as conn:
        return dict(
            conn.execute(
                "SELECT * FROM temp_batches WHERE id=?", (batch_id,)
            ).fetchone()
        )


def _drift_status(batch_id: int, status: str = "已入库") -> None:
    """模拟历史修复/导入造成的状态漂移：已有正式封批代但批次状态被改回可封。"""
    with connect() as conn:
        conn.execute(
            "UPDATE temp_batches SET status=? WHERE id=?", (status, batch_id)
        )
        conn.commit()


def _seal_once(c: TestClient, h: dict, batch_id: int) -> None:
    r = c.post("/api/seals", headers=h, json={"batch_id": batch_id})
    assert r.status_code == 200, r.text
    assert r.json()["generation"] == 2


def test_second_seal_rejects_and_zero_formal_growth():
    """甲路径：已有正式封批代时二次封批必须拒写，正式代计数零增。"""
    c = TestClient(app)
    h = _auth(c)
    batch = _seed_batch(c, h)
    _seal_once(c, h, batch["id"])

    gen_before = _batch_row(batch["id"])["generation"]
    formal_before = _formal_seals(batch["id"])
    assert gen_before == 2
    assert len(formal_before) == 1

    # 状态漂移后二次封批：当前实现会再插一正式代（红）
    _drift_status(batch["id"], "已入库")
    r2 = c.post("/api/seals", headers=h, json={"batch_id": batch["id"]})
    assert r2.status_code == 409, (
        f"二次封批应拒写 409，实际 {r2.status_code} {r2.text}"
    )

    row_after = _batch_row(batch["id"])
    formal_after = _formal_seals(batch["id"])
    assert row_after["generation"] == gen_before, "二次封批不得推进正式代"
    assert len(formal_after) == 1, "二次封批不得新增正式代"
    assert [s["batch_generation"] for s in formal_after] == [2]

    audit = c.get("/api/audit", headers=h).json()["items"]
    assert any(x["action"] == "二次封偏航" for x in audit), "偏离纪要须记二次封偏航"


def test_no_unpromoted_shadow_generation_coexists():
    """乙路径反证：不得出现未晋升的影子代草案与正式代并列。"""
    c = TestClient(app)
    h = _auth(c)
    batch = _seed_batch(c, h)
    _seal_once(c, h, batch["id"])
    _drift_status(batch["id"], "已入库")

    r2 = c.post("/api/seals", headers=h, json={"batch_id": batch["id"]})
    assert r2.status_code == 409, f"二次封批须拒写，实际 {r2.status_code}"

    rows = _all_seals(batch["id"])
    shadow = [
        s
        for s in rows
        if ("影子" in str(s["status"])) or ("草案" in str(s["status"]))
    ]
    assert shadow == [], f"不得出现影子代草案并列：{shadow}"
    assert len(rows) == 1, f"同批只许留一代，实际 {len(rows)} 代：{rows}"
    assert rows[0]["status"] == FORMAL


def test_concurrent_reseal_lock_contention_trace():
    """争用：双窗同批抢再封，后到只记争用痕，不写业务、不增代。"""
    c = TestClient(app)
    h_admin = _auth(c, "admin")
    h_ops = _auth(c, "ops")
    batch = _seed_batch(c, h_admin)
    _seal_once(c, h_admin, batch["id"])
    _drift_status(batch["id"], "已入库")
    gen_before = _batch_row(batch["id"])["generation"]
    lock_key = f"batch:{batch['id']}"

    # 甲窗口先占批次锁
    r = c.post("/api/locks/acquire", headers=h_admin, json={"lock_key": lock_key})
    assert r.status_code == 200, r.text
    try:
        # 乙窗口后到再封
        r2 = c.post("/api/seals", headers=h_ops, json={"batch_id": batch["id"]})
        assert r2.status_code == 409
        assert "争用" in r2.json()["detail"]
    finally:
        c.post("/api/locks/release", headers=h_admin, json={"lock_key": lock_key})

    cont = c.get("/api/contention", headers=h_admin).json()["items"]
    mine = [x for x in cont if x["lock_key"] == lock_key]
    assert mine, "后到争用须留痕"
    assert any(x["actor"] == "ops" for x in mine), mine

    assert _batch_row(batch["id"])["generation"] == gen_before
    assert len(_formal_seals(batch["id"])) == 1
    assert len(_all_seals(batch["id"])) == 1


def test_two_windows_simultaneous_reseal_only_one_formal():
    """双窗同时抢再封：无论谁先到，只许留一正式代且正式代不推进。"""
    c = TestClient(app)
    h_admin = _auth(c, "admin")
    h_ops = _auth(c, "ops")
    batch = _seed_batch(c, h_admin)
    _seal_once(c, h_admin, batch["id"])
    _drift_status(batch["id"], "已入库")
    gen_before = _batch_row(batch["id"])["generation"]

    barrier = threading.Barrier(2)
    codes: list[int] = []
    lock = threading.Lock()

    def fire(headers):
        barrier.wait()
        r = c.post("/api/seals", headers=headers, json={"batch_id": batch["id"]})
        with lock:
            codes.append(r.status_code)

    ts = [
        threading.Thread(target=fire, args=(h_admin,)),
        threading.Thread(target=fire, args=(h_ops,)),
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert all(sc == 409 for sc in codes), codes
    assert _batch_row(batch["id"])["generation"] == gen_before
    assert len(_all_seals(batch["id"])) == 1


def test_five_views_share_official_generation():
    """封批/批次/温谱/血缘/对账五页同源，正式代一致。"""
    c = TestClient(app)
    h = _auth(c)
    batch = _seed_batch(c, h)
    _seal_once(c, h, batch["id"])
    bid = batch["id"]
    code = batch["batch_code"]

    listed = next(
        x
        for x in c.get("/api/batches", headers=h).json()["items"]
        if x["id"] == bid
    )
    detail = c.get(f"/api/batches/{bid}", headers=h).json()["batch"]
    seals = [
        s for s in c.get("/api/seals", headers=h).json()["items"] if s["batch_id"] == bid
    ]
    proof = c.get(f"/api/proof/batch/{code}", headers=h).json()
    lineage = [
        e
        for e in c.get("/api/lineage", headers=h).json()["items"]
        if e["from_batch_id"] == bid or e["to_batch_id"] == bid
    ]

    assert listed["official_generation"] == 2
    assert detail["official_generation"] == 2
    assert len(seals) == 1 and seals[0]["official_generation"] == 2
    assert proof["official_generation"] == 2
    for e in lineage:
        if e["from_batch_id"] == bid:
            assert e["from_generation"] == 2
        if e["to_batch_id"] == bid:
            assert e["to_generation"] == 2
