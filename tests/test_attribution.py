"""分卷归属（isPartOf）端到端测试。

样例源层级（见 sample-oai/server.py）：
  rec-005 / rec-006 -> rec-028（总目，datestamp 10-28，首轮采不到）
  rec-007 -> rec-029（总目，10-29）；rec-008 -> rec-007 -> rec-029
  rec-009 -> rec-030（源中不存在）        -> parent_missing
  rec-010 -> rec-013（删除标记）           -> parent_withdrawn
  rec-011 <-> rec-012                     -> cycle
  rec-001 等无 relation                   -> root
"""
from __future__ import annotations

import json

import pytest

from app.attribution import (
    STATUS_CYCLE,
    STATUS_PARENT_MISSING,
    STATUS_PARENT_WITHDRAWN,
    STATUS_RESOLVED,
    STATUS_ROOT,
    extract_parent_identifier,
    resolve_attribution,
)


def _harvest(api, payload):
    resp = api.post("/api/v1/harvest", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _attrib(api, identifier):
    resp = api.get(f"/api/v1/attribution/{identifier}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 解析规则
# ---------------------------------------------------------------------------

def test_extract_parent_uses_only_first_ispartof():
    raw = json.dumps(
        {
            "relation": [
                "references: 普通关联应忽略",
                "isPartOf: oai:sample.lib:rec-028",
                "isPartOf: oai:sample.lib:rec-029",  # 至多一项，第二项忽略
            ]
        },
        ensure_ascii=False,
    )
    assert extract_parent_identifier(raw) == "oai:sample.lib:rec-028"


def test_extract_parent_case_insensitive_and_blank():
    assert (
        extract_parent_identifier(
            json.dumps({"relation": ["ispartof:  oai:x:1  "]})
        )
        == "oai:x:1"
    )
    assert extract_parent_identifier(json.dumps({"relation": []})) is None
    assert extract_parent_identifier(
        json.dumps({"relation": ["isPartOf:   "]})
    ) is None
    assert extract_parent_identifier("{}") is None


# ---------------------------------------------------------------------------
# 先采分卷：上级未采到 -> 未决；补采上级后自动重算为已归属
# ---------------------------------------------------------------------------

def test_volume_harvested_before_parent_is_then_resolved(client):
    api, _db, _oai = client

    # 首轮只采到 10-15：分卷 rec-005/006 在架，总目 rec-028 尚未采到
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})

    missing = _attrib(api, "oai:sample.lib:rec-005")
    assert missing["status"] == STATUS_PARENT_MISSING
    assert missing["root_identifier"] is None
    assert missing["parent_identifier"] == "oai:sample.lib:rec-028"
    assert missing["chain"] == [
        "oai:sample.lib:rec-005",
        "oai:sample.lib:rec-028",
    ]
    assert "缺失" in missing["reason"]

    # 未决清单中应能查到
    unresolved = api.get("/api/v1/attributions/unresolved").json()
    ids = {row["identifier"] for row in unresolved}
    assert "oai:sample.lib:rec-005" in ids
    assert "oai:sample.lib:rec-006" in ids

    # 补采上级（区间覆盖 rec-028 的 10-28）
    result = _harvest(api, {"until": "2026-10-29"})
    # 本批 10-15..10-29 的记录 + 以它们为上级的旧卷册 rec-005/006/007/008
    assert result["attributions_recomputed"] > 0

    resolved = _attrib(api, "oai:sample.lib:rec-005")
    assert resolved["status"] == STATUS_RESOLVED
    assert resolved["root_identifier"] == "oai:sample.lib:rec-028"
    assert resolved["parent_identifier"] == "oai:sample.lib:rec-028"
    assert resolved["chain"] == [
        "oai:sample.lib:rec-005",
        "oai:sample.lib:rec-028",
    ]

    # 未决清单中 rec-005/006 消失
    unresolved_after = api.get("/api/v1/attributions/unresolved").json()
    ids_after = {row["identifier"] for row in unresolved_after}
    assert "oai:sample.lib:rec-005" not in ids_after
    assert "oai:sample.lib:rec-006" not in ids_after


def test_multi_level_chain_resolves_to_top_root(client):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-29"})

    deep = _attrib(api, "oai:sample.lib:rec-008")
    assert deep["status"] == STATUS_RESOLVED
    assert deep["root_identifier"] == "oai:sample.lib:rec-029"
    assert deep["parent_identifier"] == "oai:sample.lib:rec-007"
    assert deep["chain"] == [
        "oai:sample.lib:rec-008",
        "oai:sample.lib:rec-007",
        "oai:sample.lib:rec-029",
    ]

    root = _attrib(api, "oai:sample.lib:rec-029")
    assert root["status"] == STATUS_ROOT
    assert root["root_identifier"] == "oai:sample.lib:rec-029"
    assert root["parent_identifier"] is None
    assert root["chain"] == ["oai:sample.lib:rec-029"]


def test_plain_record_without_relation_is_root(client):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    rec = _attrib(api, "oai:sample.lib:rec-001")
    assert rec["status"] == STATUS_ROOT
    assert rec["record_status"] == "active"
    assert rec["root_identifier"] == "oai:sample.lib:rec-001"


def test_missing_parent_remains_unresolved(client):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-29"})
    rec = _attrib(api, "oai:sample.lib:rec-009")
    assert rec["status"] == STATUS_PARENT_MISSING
    assert rec["parent_identifier"] == "oai:sample.lib:rec-030"
    assert rec["root_identifier"] is None


def test_withdrawn_parent_reported_with_reason(client):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    rec = _attrib(api, "oai:sample.lib:rec-010")
    assert rec["status"] == STATUS_PARENT_WITHDRAWN
    assert rec["parent_identifier"] == "oai:sample.lib:rec-013"
    assert "撤下" in rec["reason"]
    # 卷册自身仍在架
    assert rec["record_status"] == "active"


def test_cycle_detected_with_chain_loop(client):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    rec = _attrib(api, "oai:sample.lib:rec-011")
    assert rec["status"] == STATUS_CYCLE
    assert rec["root_identifier"] is None
    # 链尾重复环入口，编目员可直接看出环
    assert rec["chain"][0] == "oai:sample.lib:rec-011"
    assert rec["chain"][-1] == "oai:sample.lib:rec-011"
    assert "成环" in rec["reason"]

    other = _attrib(api, "oai:sample.lib:rec-012")
    assert other["status"] == STATUS_CYCLE


def test_attribution_unknown_identifier_returns_404(client):
    api, _db, _oai = client
    resp = api.get("/api/v1/attribution/oai:sample.lib:nope")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 增量重算：只动本批与受影响下级，不重扫全库
# ---------------------------------------------------------------------------

def test_recompute_scope_is_batch_plus_descendants(client, monkeypatch):
    api, db_mod, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})

    # 统计补采 rec-028 前后的归属 updated_at：无关总目不应被重算
    conn = db_mod.get_connection()
    try:
        untouched_before = conn.execute(
            "SELECT updated_at FROM attributions WHERE identifier = ?",
            ("oai:sample.lib:rec-001",),
        ).fetchone()["updated_at"]
    finally:
        conn.close()

    calls: list[str] = []
    real_resolve = resolve_attribution

    def spy_resolve(c, identifier):
        calls.append(identifier)
        return real_resolve(c, identifier)

    import app.harvester as harvester_mod
    monkeypatch.setattr(harvester_mod, "resolve_attribution", spy_resolve)

    # 本批只含 rec-028（10-15 含当天无更新，窗口内实际仅 rec-028 等新记录）
    _harvest(api, {"until": "2026-10-28"})

    # rec-001 不在本批、也不是 rec-028 的下级 -> 不重算
    assert "oai:sample.lib:rec-001" not in calls
    # rec-005/006 以 rec-028 为上级 -> 受影响必须重算
    assert "oai:sample.lib:rec-005" in calls
    assert "oai:sample.lib:rec-006" in calls

    conn = db_mod.get_connection()
    try:
        untouched_after = conn.execute(
            "SELECT updated_at FROM attributions WHERE identifier = ?",
            ("oai:sample.lib:rec-001",),
        ).fetchone()["updated_at"]
    finally:
        conn.close()
    assert untouched_before == untouched_after


def test_failed_harvest_rolls_back_attributions(client, monkeypatch):
    api, db_mod, oai_mod = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})

    before = api.get(
        "/api/v1/attribution/oai:sample.lib:rec-005"
    ).json()
    assert before["status"] == STATUS_PARENT_MISSING

    # 让采集在翻页阶段失败 -> 整批回滚，归属与截止日都不变
    real_consume = oai_mod.TOKENS.consume

    def always_invalid(_token):
        real_consume(_token)
        return None

    monkeypatch.setattr(oai_mod.TOKENS, "consume", always_invalid)
    resp = api.post(
        "/api/v1/harvest",
        json={"from": "2026-10-01", "until": "2026-10-29"},
    )
    assert resp.status_code == 502

    after = api.get(
        "/api/v1/attribution/oai:sample.lib:rec-005"
    ).json()
    assert after["status"] == before["status"]
    assert after["updated_at"] == before["updated_at"]

    conn = db_mod.get_connection()
    try:
        assert db_mod.get_last_until(conn) == "2026-10-15"
        # rec-028 不得因失败批次入库
        assert conn.execute(
            "SELECT identifier FROM bib_records WHERE identifier = ?",
            ("oai:sample.lib:rec-028",),
        ).fetchone() is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 旧库升级：用旧目录补建归属，不碰续采进度
# ---------------------------------------------------------------------------

def test_backfill_builds_attributions_without_touching_progress(client):
    api, db_mod, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})

    conn = db_mod.get_connection()
    try:
        # 模拟旧库：删掉归属表，保留目录与进度
        conn.execute("DROP TABLE attributions")
        conn.commit()
        progress_before = conn.execute(
            "SELECT last_until, last_harvest_at FROM harvest_progress WHERE id = 1"
        ).fetchone()
        catalog_count = conn.execute(
            "SELECT COUNT(*) FROM bib_records"
        ).fetchone()[0]
    finally:
        conn.close()

    # 重新初始化（模拟服务升级重启）
    db_mod.init_db()

    conn = db_mod.get_connection()
    try:
        progress_after = conn.execute(
            "SELECT last_until, last_harvest_at FROM harvest_progress WHERE id = 1"
        ).fetchone()
        assert dict(progress_after) == dict(progress_before)
        attr_count = conn.execute(
            "SELECT COUNT(*) FROM attributions"
        ).fetchone()[0]
        assert attr_count == catalog_count  # 旧目录全部补建

        row = conn.execute(
            "SELECT status, root_identifier FROM attributions WHERE identifier = ?",
            ("oai:sample.lib:rec-001",),
        ).fetchone()
        assert row["status"] == STATUS_ROOT
        assert row["root_identifier"] == "oai:sample.lib:rec-001"
    finally:
        conn.close()

    # 升级后 API 直接可查，无需重新采集
    rec = _attrib(api, "oai:sample.lib:rec-011")
    assert rec["status"] == STATUS_CYCLE


def test_reinit_is_idempotent_and_does_not_recompute(client):
    api, db_mod, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    conn = db_mod.get_connection()
    try:
        before = conn.execute(
            "SELECT identifier, updated_at FROM attributions"
        ).fetchall()
        before_map = {r["identifier"]: r["updated_at"] for r in before}
    finally:
        conn.close()

    db_mod.init_db()  # 再次启动：无缺失行，不应覆盖任何归属

    conn = db_mod.get_connection()
    try:
        after = conn.execute(
            "SELECT identifier, updated_at FROM attributions"
        ).fetchall()
        after_map = {r["identifier"]: r["updated_at"] for r in after}
    finally:
        conn.close()
    assert before_map == after_map


# ---------------------------------------------------------------------------
# 直接单元级求解：墓碑与撤下链顶
# ---------------------------------------------------------------------------

def test_withdrawn_start_record_without_parent_is_root(client):
    api, db_mod, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    conn = db_mod.get_connection()
    try:
        result = resolve_attribution(conn, "oai:sample.lib:rec-013")
        assert result.status == STATUS_ROOT
        assert result.root_identifier == "oai:sample.lib:rec-013"
    finally:
        conn.close()
    # API 响应同时体现卷册已撤下
    rec = _attrib(api, "oai:sample.lib:rec-013")
    assert rec["record_status"] == "withdrawn"
    assert rec["status"] == STATUS_ROOT


def test_self_referential_is_cycle(client):
    api, db_mod, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    conn = db_mod.get_connection()
    try:
        # 手工构造自环：rec-002 声明上级为自身
        conn.execute(
            "UPDATE bib_records SET raw_dc = ? WHERE identifier = ?",
            (
                json.dumps(
                    {"relation": [f"isPartOf: oai:sample.lib:rec-002"]},
                    ensure_ascii=False,
                ),
                "oai:sample.lib:rec-002",
            ),
        )
        conn.commit()
        result = resolve_attribution(conn, "oai:sample.lib:rec-002")
        assert result.status == STATUS_CYCLE
        assert result.chain == [
            "oai:sample.lib:rec-002",
            "oai:sample.lib:rec-002",
        ]
    finally:
        conn.close()


def test_lazy_compute_when_attribution_row_missing(client):
    api, db_mod, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    conn = db_mod.get_connection()
    try:
        conn.execute(
            "DELETE FROM attributions WHERE identifier = ?",
            ("oai:sample.lib:rec-001",),
        )
        conn.commit()
    finally:
        conn.close()

    rec = _attrib(api, "oai:sample.lib:rec-001")
    assert rec["status"] == STATUS_ROOT

    # 查询后归属行已补写
    conn = db_mod.get_connection()
    try:
        row = db_mod.get_attribution(conn, "oai:sample.lib:rec-001")
        assert row is not None and row["status"] == STATUS_ROOT
    finally:
        conn.close()
