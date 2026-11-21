"""断边死信验收：死信标记、隔离条数、表内凭证、指纹拦截、并发串行闸。"""
import sqlite3
import threading

import pytest

from app.services import coverage, deadletter, edges, impact, nodes, settings
from app.services.errors import ServiceError


def _edge_by_code(items, code):
    return next(e for e in items if e["edge_code"] == code)


def test_seed_has_dead_letter_vouchers_and_marks(db_path):
    vouchers = deadletter.list_vouchers(db_path=db_path)
    open_ones = [v for v in vouchers if v["status"] == "待核齐"]
    assert len(open_ones) == 2
    sources = {v["source"] for v in open_ones}
    assert sources == {"人工送死信", "扫描断边"}
    # 死信页显示命运来源与边指纹
    for v in open_ones:
        assert v["fingerprint"] and len(v["fingerprint"]) == 64
        assert v["short_fingerprint"] == v["fingerprint"][:12]
        assert v["source"] in ("人工送死信", "扫描断边")
    # 主表断边带死信标记
    edge_rows = edges.list_edges(db_path=db_path)
    broken = _edge_by_code(edge_rows, "E-BROKEN-1")
    assert broken["status"] == "已死信"
    # 扫描孤儿凭证挂不上主表边
    ghost = next(v for v in open_ones if v["edge_code"] == "E-BROKEN-2")
    assert ghost["edge_id"] is None


def test_dead_edges_are_isolated_from_impact_and_coverage(db_path):
    root = next(n for n in nodes.list_nodes(db_path=db_path)
                if n["node_code"] == "N-DWD-ORD")
    job = impact.start_impact(int(root["id"]), db_path=db_path)
    hit_codes = {
        h["node_code"] for h in impact.job_detail(job["id"], db_path=db_path)["hits"]
    }
    # 停用的 N-ODS-LEGACY 绝不沿死信边混入命中
    assert "N-ODS-LEGACY" not in hit_codes
    # 影响页展示隔离条数
    listed = impact.list_jobs(db_path=db_path)
    assert listed[0]["isolated_count"] == 2
    # 覆盖：死信边不计入已发布边，且不虚绿
    rep = coverage.run_coverage(db_path=db_path)
    assert rep["dead_letter_count"] == 1
    assert rep["edge_count"] == 6
    assert "死信隔离" in rep["summary"]
    assert coverage.latest_chip(db_path) == "死信待核"


def test_healthy_edge_cannot_be_sent_to_dead_letter(db_path):
    healthy = next(e for e in edges.list_edges(db_path=db_path)
                   if e["status"] == "已发布")
    with pytest.raises(ServiceError, match="不是断边"):
        deadletter.send_edge_to_dead_letter(healthy["id"], db_path=db_path)


def test_manual_send_writes_voucher_and_marks_edge(db_path):
    # 造一条挂停用节点的活边
    conn = sqlite3.connect(db_path)
    legacy = conn.execute(
        "SELECT id FROM lineage_nodes WHERE node_code='N-ODS-LEGACY'"
    ).fetchone()[0]
    target = conn.execute(
        "SELECT id FROM lineage_nodes WHERE node_code='N-DWD-PAY'"
    ).fetchone()[0]
    eid = conn.execute(
        "INSERT INTO lineage_edges(edge_code, upstream_id, downstream_id, "
        "transform_kind, status) VALUES ('E-MANUAL',?,?,'直抽','已发布')",
        (legacy, target),
    ).lastrowid
    conn.commit()
    conn.close()

    before = deadletter.open_count(db_path)
    voucher = deadletter.send_edge_to_dead_letter(eid, "值班发现上游停用", db_path=db_path)
    assert voucher["source"] == "人工送死信"
    assert voucher["status"] == "待核齐"
    assert voucher["edge_id"] == eid
    assert deadletter.open_count(db_path) == before + 1
    assert _edge_by_code(edges.list_edges(db_path=db_path), "E-MANUAL")["status"] == "已死信"
    # 死信边禁止发布/作废
    with pytest.raises(ServiceError, match="死信"):
        edges.publish_edge(eid, db_path=db_path)
    with pytest.raises(ServiceError, match="死信"):
        edges.void_edge(eid, db_path=db_path)


def test_race_two_operators_single_voucher(db_path):
    conn = sqlite3.connect(db_path)
    nid = conn.execute(
        "INSERT INTO lineage_nodes(node_code, table_name, layer, owner_team, status) "
        "VALUES ('N-RACE','t_race','ODS','x','停用')"
    ).lastrowid
    dwd = conn.execute(
        "SELECT id FROM lineage_nodes WHERE node_code='N-DWD-ORD'"
    ).fetchone()[0]
    eid = conn.execute(
        "INSERT INTO lineage_edges(edge_code, upstream_id, downstream_id, "
        "transform_kind, status) VALUES ('E-RACE',?,?,'直抽','已发布')",
        (nid, dwd),
    ).lastrowid
    conn.commit()
    conn.close()

    errors = []

    def worker():
        try:
            deadletter.send_edge_to_dead_letter(eid, db_path=db_path)
        except Exception as exc:  # 串行闸可能抛业务错或唯一索引错，均应只落一张
            errors.append(str(getattr(exc, "message", exc)))
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = [v for v in deadletter.list_vouchers(db_path=db_path)
            if v["edge_id"] == eid and v["status"] == "待核齐"]
    assert len(rows) == 1  # 无串行闸时会双写，这里只能一张
    assert len(errors) == 1
    assert "重复" in errors[0] or "串行闸" in errors[0]
    assert _edge_by_code(edges.list_edges(db_path=db_path), "E-RACE")["status"] == "已死信"


def test_scan_ingest_blocks_fingerprint_mismatch(db_path):
    # E-01 现行变换是「清洗」，扫描 payload 故意改成别的 -> 指纹对不上 -> 拦截
    report = deadletter.scan_ingest([
        {
            "edge_code": "E-01",
            "upstream_code": "N-ODS-ORD",
            "downstream_code": "N-DWD-ORD",
            "transform_kind": "被篡改",
        }
    ], db_path=db_path)
    assert report["blocked_count"] == 1
    assert report["ingested_count"] == 0
    assert "指纹" in report["blocked"][0]["reason"]
    # 现行边纹丝不动
    assert _edge_by_code(edges.list_edges(db_path=db_path), "E-01")["status"] == "已发布"


def test_scan_ingest_routes_broken_and_healthy_and_is_idempotent(db_path):
    report = deadletter.scan_ingest([
        {  # 健康边：入草稿
            "edge_code": "E-SCAN-OK",
            "upstream_code": "N-ODS-PAY",
            "downstream_code": "N-ADS-ALERT",
            "transform_kind": "直抽",
        },
        {  # 断边：目录无此节点 -> 死信凭证（edge_id 为空）
            "edge_code": "E-SCAN-GHOST",
            "upstream_code": "N-GHOST-9",
            "downstream_code": "N-DWS-REV",
        },
    ], db_path=db_path)
    assert report["ingested_count"] == 1
    assert report["dead_count"] == 1
    ok = _edge_by_code(edges.list_edges(db_path=db_path), "E-SCAN-OK")
    assert ok["status"] == "草稿"
    voucher = next(v for v in deadletter.list_vouchers(db_path=db_path)
                   if v["edge_code"] == "E-SCAN-GHOST")
    assert voucher["source"] == "扫描断边"
    assert voucher["edge_id"] is None

    # 同指纹再扫一次：幂等，不双写凭证
    again = deadletter.scan_ingest([
        {
            "edge_code": "E-SCAN-GHOST",
            "upstream_code": "N-GHOST-9",
            "downstream_code": "N-DWS-REV",
        }
    ], db_path=db_path)
    assert again["dead_count"] == 1
    dupes = [v for v in deadletter.list_vouchers(db_path=db_path)
             if v["edge_code"] == "E-SCAN-GHOST" and v["status"] == "待核齐"]
    assert len(dupes) == 1


def test_reconcile_revives_edge_and_closes_voucher(db_path):
    conn = sqlite3.connect(db_path)
    legacy = conn.execute(
        "SELECT id FROM lineage_nodes WHERE node_code='N-ODS-LEGACY'"
    ).fetchone()[0]
    conn.execute("UPDATE lineage_nodes SET status='在用' WHERE id=?", (legacy,))
    conn.commit()
    conn.close()

    voucher = next(v for v in deadletter.list_vouchers(db_path=db_path)
                   if v["edge_code"] == "E-BROKEN-1")
    out = deadletter.reconcile_voucher(voucher["id"], "节点已恢复", db_path=db_path)
    assert out["status"] == "已核齐"
    assert out["revived_edge_id"] == voucher["edge_id"]
    edge = _edge_by_code(edges.list_edges(db_path=db_path), "E-BROKEN-1")
    assert edge["status"] == "草稿"  # 复活后回到草稿，须重新发布


def test_reconcile_blocked_while_endpoint_retired(db_path):
    voucher = next(v for v in deadletter.list_vouchers(db_path=db_path)
                   if v["edge_code"] == "E-BROKEN-1")
    with pytest.raises(ServiceError, match="端点"):
        deadletter.reconcile_voucher(voucher["id"], db_path=db_path)


def test_mode_setting_is_locked(db_path):
    with pytest.raises(ServiceError, match="不可并存"):
        settings.update_setting("dead_letter_mode", "独立队列吞没", db_path=db_path)
    values = {s["key"]: s["value"] for s in settings.list_settings(db_path=db_path)}
    assert values["dead_letter_mode"] == deadletter.DEAD_LETTER_MODE
