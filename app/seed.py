"""初始化样例血缘节点、边、台参数。"""
from __future__ import annotations

from app.db import SCHEMA, get_db, init_schema
from app.services.deadletter import DEAD_LETTER_MODE, compute_fingerprint, short_fp

SCHEMA_VERSION = "lineage-v2"


def _schema_compatible(conn) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='lineage_nodes'"
    ).fetchone()
    if not row:
        return False
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(lineage_nodes)").fetchall()
    }
    return "node_code" in cols and "layer" in cols


def seed_if_needed(db_path: str | None = None) -> dict:
    init_schema(db_path)
    with get_db(db_path) as conn:
        ver = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if not _schema_compatible(conn) or (ver and ver["value"] != SCHEMA_VERSION):
            for table in (
                "notify_logs",
                "impact_hits",
                "impact_jobs",
                "dead_letter_vouchers",
                "coverage_reports",
                "lineage_edges",
                "lineage_nodes",
                "lab_settings",
                "meta",
            ):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.executescript(SCHEMA)
        elif conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            return {"seeded": False, "reason": "already"}

        nodes = [
            ("N-ODS-ORD", "ods_orders", "ODS", "采集组", "订单原始"),
            ("N-ODS-PAY", "ods_payments", "ODS", "采集组", "支付原始"),
            ("N-DWD-ORD", "dwd_orders", "DWD", "仓建组", "订单明细"),
            ("N-DWD-PAY", "dwd_payments", "DWD", "仓建组", "支付明细"),
            ("N-DWS-REV", "dws_revenue", "DWS", "分析组", "收入汇总"),
            ("N-ADS-DASH", "ads_dash_rev", "ADS", "分析组", "收入看板"),
            ("N-ADS-ALERT", "ads_alert_rev", "ADS", "值班组", "收入告警"),
        ]
        ids = {}
        for code, table, layer, team, note in nodes:
            cur = conn.execute(
                "INSERT INTO lineage_nodes(node_code, table_name, layer, owner_team, status, note) "
                "VALUES (?,?,?,?,'在用',?)",
                (code, table, layer, team, note),
            )
            ids[code] = cur.lastrowid

        edges = [
            ("E-01", "N-ODS-ORD", "N-DWD-ORD", "清洗"),
            ("E-02", "N-ODS-PAY", "N-DWD-PAY", "清洗"),
            ("E-03", "N-DWD-ORD", "N-DWS-REV", "汇总"),
            ("E-04", "N-DWD-PAY", "N-DWS-REV", "汇总"),
            ("E-05", "N-DWS-REV", "N-ADS-DASH", "直抽"),
            ("E-06", "N-DWS-REV", "N-ADS-ALERT", "直抽"),
        ]
        for code, up, down, kind in edges:
            conn.execute(
                "INSERT INTO lineage_edges(edge_code, upstream_id, downstream_id, "
                "transform_kind, status, note) VALUES (?,?,?,?,'已发布','')",
                (code, ids[up], ids[down], kind),
            )
        # one draft edge for coverage gap demo
        conn.execute(
            "INSERT INTO lineage_edges(edge_code, upstream_id, downstream_id, "
            "transform_kind, status, note) VALUES ('E-DRAFT',?,?,'直抽','草稿','待审')"
            ,
            (ids["N-ODS-ORD"], ids["N-ADS-DASH"]),
        )

        # ---- 断边死信样例（旁路账册模式）----
        # 1) 停用节点 + 挂在它身上的已死信边（人工送死信，凭证挂主表边）
        cur = conn.execute(
            "INSERT INTO lineage_nodes(node_code, table_name, layer, owner_team, status, note) "
            "VALUES ('N-ODS-LEGACY','ods_legacy_snap','ODS','采集组','停用','历史快照已下线')"
        )
        legacy_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO lineage_edges(edge_code, upstream_id, downstream_id, "
            "transform_kind, status, note) VALUES ('E-BROKEN-1',?,?,'直抽','已死信','下游已停用')",
            (legacy_id, ids["N-DWD-ORD"]),
        )
        broken_edge_id = cur.lastrowid
        fp1 = compute_fingerprint("E-BROKEN-1", "N-ODS-LEGACY", "N-DWD-ORD", "直抽")
        conn.execute(
            "INSERT INTO dead_letter_vouchers(voucher_code, edge_id, edge_code, "
            "upstream_ref, downstream_ref, transform_kind, fingerprint, short_fingerprint, "
            "reason, source, status, created_by) "
            "VALUES ('DL-0001',?,'E-BROKEN-1','N-ODS-LEGACY','N-DWD-ORD','直抽',?,?,"
            "'上游节点已停用','人工送死信','待核齐','admin')",
            (broken_edge_id, fp1, short_fp(fp1)),
        )
        # 2) 扫描发现、目录里根本没有的节点（扫描断边，凭证挂不上主表边）
        fp2 = compute_fingerprint("E-BROKEN-2", "N-GHOST-TBL", "N-DWS-REV", "清洗")
        conn.execute(
            "INSERT INTO dead_letter_vouchers(voucher_code, edge_id, edge_code, "
            "upstream_ref, downstream_ref, transform_kind, fingerprint, short_fingerprint, "
            "reason, source, status, created_by) "
            "VALUES ('DL-0002',NULL,'E-BROKEN-2','N-GHOST-TBL','N-DWS-REV','清洗',?,?,"
            "'扫描引用了目录中不存在或已停用的节点','扫描断边','待核齐','admin')",
            (fp2, short_fp(fp2)),
        )

        conn.execute(
            "INSERT INTO lab_settings(key, value, label) VALUES "
            "('impact_max_hop','8','影响最大跳数'),"
            "('notify_channel','站内','默认通知渠道'),"
            "('coverage_mode','严格','覆盖对账模式'),"
            f"('dead_letter_mode','{DEAD_LETTER_MODE}','断边处理命运（只读，独立队列吞没不可并存）')"
        )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.execute("INSERT INTO meta(key, value) VALUES ('lab','表血缘影响台')")
        return {
            "seeded": True,
            "nodes": len(nodes) + 1,
            "edges": len(edges) + 2,
            "dead_letters": 2,
            "settings": 4,
        }


def reset_seed(db_path: str | None = None) -> None:
    init_schema(db_path)
    with get_db(db_path) as conn:
        for table in (
            "notify_logs",
            "impact_hits",
            "impact_jobs",
            "dead_letter_vouchers",
            "coverage_reports",
            "lineage_edges",
            "lineage_nodes",
            "lab_settings",
            "meta",
        ):
            conn.execute(f"DELETE FROM {table}")
    seed_if_needed(db_path)
