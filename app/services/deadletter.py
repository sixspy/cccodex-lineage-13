"""断边死信：旁路账册模式（唯一兑现的命运）。

断边不被独立队列吞没：主表 lineage_edges 置「已死信」标记并从影响/覆盖统计隔离，
同时在 dead_letter_vouchers 写死信凭证，记全量边指纹供日后核齐。
串行闸：get_db_immediate 取 BEGIN IMMEDIATE 写锁 + 待核齐唯一索引。
"""
from __future__ import annotations

import hashlib

from app.db import get_db, get_db_immediate
from app.services.errors import ServiceError

# 命运只兑现一种，另写在台参数里明示不可切换
DEAD_LETTER_MODE = "旁路账册死信凭证"
FORBIDDEN_MODE = "独立队列吞没"


def compute_fingerprint(edge_code: str, upstream_ref: str, downstream_ref: str,
                        transform_kind: str) -> str:
    """边指纹：边号 + 上下游目录编码 + 变换，全量 sha256。"""
    payload = "|".join([
        (edge_code or "").strip(),
        (upstream_ref or "").strip(),
        (downstream_ref or "").strip(),
        (transform_kind or "直抽").strip(),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def short_fp(fingerprint: str) -> str:
    return (fingerprint or "")[:12]


def _edge_current_fingerprint(conn, edge) -> str:
    up = conn.execute(
        "SELECT node_code FROM lineage_nodes WHERE id=?", (edge["upstream_id"],)
    ).fetchone()
    down = conn.execute(
        "SELECT node_code FROM lineage_nodes WHERE id=?", (edge["downstream_id"],)
    ).fetchone()
    if not up or not down:
        raise ServiceError("现行边的上下游节点缺失，无法核算指纹")
    return compute_fingerprint(
        edge["edge_code"], up["node_code"], down["node_code"], edge["transform_kind"]
    )


def _is_broken(conn, edge) -> tuple[bool, str]:
    for nid, label in ((edge["upstream_id"], "上游"), (edge["downstream_id"], "下游")):
        node = conn.execute(
            "SELECT status FROM lineage_nodes WHERE id=?", (nid,)
        ).fetchone()
        if not node:
            return True, f"{label}节点已不存在"
        if node["status"] == "停用":
            return True, f"{label}节点已停用"
    return False, ""


def _next_voucher_code(conn) -> str:
    row = conn.execute("SELECT COUNT(*) AS c FROM dead_letter_vouchers").fetchone()
    return f"DL-{int(row['c']) + 1:04d}"


def _insert_voucher(conn, *, edge_id, edge_code, upstream_ref, downstream_ref,
                    transform_kind, fingerprint, reason, source, created_by) -> dict:
    code = _next_voucher_code(conn)
    try:
        cur = conn.execute(
            "INSERT INTO dead_letter_vouchers(voucher_code, edge_id, edge_code, "
            "upstream_ref, downstream_ref, transform_kind, fingerprint, short_fingerprint, "
            "reason, source, status, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, '待核齐', ?)",
            (code, edge_id, edge_code, upstream_ref, downstream_ref, transform_kind,
             fingerprint, short_fp(fingerprint), reason, source, created_by or "admin"),
        )
    except Exception as exc:
        # 串行闸唯一索引撞库：并发下后到者在此被挡，不双写凭证
        raise ServiceError(
            "该断边指纹已有待核齐凭证（串行闸拦截，未重复落库）", 409
        ) from exc
    return dict(
        conn.execute(
            "SELECT * FROM dead_letter_vouchers WHERE id=?", (cur.lastrowid,)
        ).fetchone()
    )


def list_vouchers(status: str | None = None, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        sql = (
            "SELECT v.*, e.status AS edge_status FROM dead_letter_vouchers v "
            "LEFT JOIN lineage_edges e ON e.id=v.edge_id "
        )
        if status:
            rows = conn.execute(
                sql + " WHERE v.status=? ORDER BY v.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(sql + " ORDER BY v.id DESC").fetchall()
        return [dict(r) for r in rows]


def open_count(db_path: str | None = None) -> int:
    """影响页展示的隔离条数：待核齐凭证数。"""
    with get_db(db_path) as conn:
        return int(conn.execute(
            "SELECT COUNT(*) AS c FROM dead_letter_vouchers WHERE status='待核齐'"
        ).fetchone()["c"])


def send_edge_to_dead_letter(edge_id: int, reason: str = "",
                             created_by: str = "admin",
                             db_path: str | None = None) -> dict:
    """值班手动把一条边送死信：一次落库（串行闸内完成校验+标记+凭证）。"""
    reason = (reason or "").strip()
    with get_db_immediate(db_path) as conn:
        edge = conn.execute(
            "SELECT * FROM lineage_edges WHERE id=?", (edge_id,)
        ).fetchone()
        if not edge:
            raise ServiceError("边不存在", 404)
        if edge["status"] == "已死信":
            raise ServiceError("该边已在死信，不可重复送", 409)
        if edge["status"] == "已作废":
            raise ServiceError("已作废边不可送死信", 409)
        broken, why = _is_broken(conn, edge)
        if not broken:
            raise ServiceError("上下游节点均有效，不是断边，不可送死信", 409)
        fingerprint = _edge_current_fingerprint(conn, edge)
        dup = conn.execute(
            "SELECT id FROM dead_letter_vouchers WHERE fingerprint=? AND status='待核齐'",
            (fingerprint,),
        ).fetchone()
        if dup:
            raise ServiceError("该断边指纹已有待核齐死信凭证，不可重复送", 409)
        if not reason:
            reason = why
        conn.execute(
            "UPDATE lineage_edges SET status='已死信' WHERE id=?", (edge_id,)
        )
        up = conn.execute(
            "SELECT node_code FROM lineage_nodes WHERE id=?", (edge["upstream_id"],)
        ).fetchone()
        down = conn.execute(
            "SELECT node_code FROM lineage_nodes WHERE id=?", (edge["downstream_id"],)
        ).fetchone()
        voucher = _insert_voucher(
            conn,
            edge_id=edge_id,
            edge_code=edge["edge_code"],
            upstream_ref=up["node_code"] if up else "",
            downstream_ref=down["node_code"] if down else "",
            transform_kind=edge["transform_kind"],
            fingerprint=fingerprint,
            reason=reason,
            source="人工送死信",
            created_by=created_by,
        )
        return voucher


def scan_ingest(items: list[dict], created_by: str = "admin",
                db_path: str | None = None) -> dict:
    """扫描结果去重入库：健康边进主表，断边写死信凭证，指纹对不上现行边即拦截。"""
    if not isinstance(items, list) or not items:
        raise ServiceError("扫描清单不能为空")
    ingested, dead, blocked = [], [], []
    with get_db_immediate(db_path) as conn:
        for raw in items:
            edge_code = str(raw.get("edge_code") or "").strip()
            upstream_ref = str(raw.get("upstream_code") or "").strip()
            downstream_ref = str(raw.get("downstream_code") or "").strip()
            transform_kind = str(raw.get("transform_kind") or "直抽").strip() or "直抽"
            if not edge_code or not upstream_ref or not downstream_ref:
                blocked.append({"edge_code": edge_code, "reason": "边号与上下游编码必填"})
                continue
            fingerprint = compute_fingerprint(
                edge_code, upstream_ref, downstream_ref, transform_kind
            )
            up = conn.execute(
                "SELECT * FROM lineage_nodes WHERE node_code=?", (upstream_ref,)
            ).fetchone()
            down = conn.execute(
                "SELECT * FROM lineage_nodes WHERE node_code=?", (downstream_ref,)
            ).fetchone()
            existing = conn.execute(
                "SELECT * FROM lineage_edges WHERE edge_code=?", (edge_code,)
            ).fetchone()
            open_voucher = conn.execute(
                "SELECT id FROM dead_letter_vouchers "
                "WHERE fingerprint=? AND status='待核齐'",
                (fingerprint,),
            ).fetchone()

            # 1) 指纹对不上现行边：拦住去重入库
            if existing and existing["status"] != "已死信":
                current_fp = _edge_current_fingerprint(conn, existing)
                if current_fp != fingerprint:
                    blocked.append({
                        "edge_code": edge_code,
                        "reason": "断边指纹与现行边对不上，已拦住去重入库，请人工核齐",
                    })
                    continue

            # 2) 已有待核齐凭证：幂等跳过，不双写
            if open_voucher:
                dead.append({"edge_code": edge_code, "voucher": "已待核齐"})
                continue

            # 3) 断边：旁路账册写凭证（可能挂不上主表边）
            broken = (not up or not down
                      or up["status"] == "停用" or down["status"] == "停用")
            if broken:
                if existing:
                    conn.execute(
                        "UPDATE lineage_edges SET status='已死信' WHERE id=?",
                        (existing["id"],),
                    )
                    edge_id = existing["id"]
                    reason = "扫描发现端点失效"
                else:
                    edge_id = None
                    reason = "扫描引用了目录中不存在或已停用的节点"
                voucher = _insert_voucher(
                    conn,
                    edge_id=edge_id,
                    edge_code=edge_code,
                    upstream_ref=upstream_ref,
                    downstream_ref=downstream_ref,
                    transform_kind=transform_kind,
                    fingerprint=fingerprint,
                    reason=reason,
                    source="扫描断边",
                    created_by=created_by,
                )
                dead.append({"edge_code": edge_code, "voucher": voucher["voucher_code"]})
                continue

            # 4) 健康边去重入库
            if existing:
                ingested.append({"edge_code": edge_code, "result": "已存在跳过"})
                continue
            pair = conn.execute(
                "SELECT edge_code FROM lineage_edges "
                "WHERE upstream_id=? AND downstream_id=? AND status!='已死信'",
                (up["id"], down["id"]),
            ).fetchone()
            if pair:
                blocked.append({
                    "edge_code": edge_code,
                    "reason": f"上下游端点已被现行边 {pair['edge_code']} 占用，已拦住去重入库",
                })
                continue
            try:
                cur = conn.execute(
                    "INSERT INTO lineage_edges(edge_code, upstream_id, downstream_id, "
                    "transform_kind, status, note) VALUES (?,?,?,?,'草稿','扫描入库')",
                    (edge_code, up["id"], down["id"], transform_kind),
                )
            except Exception as exc:  # 端点对等撞库
                blocked.append({"edge_code": edge_code, "reason": f"入库失败，请人工核对（{exc}）"})
                continue
            ingested.append({"edge_code": edge_code, "edge_id": cur.lastrowid})

    return {
        "mode": DEAD_LETTER_MODE,
        "ingested": ingested,
        "dead": dead,
        "blocked": blocked,
        "ingested_count": len(ingested),
        "dead_count": len(dead),
        "blocked_count": len(blocked),
    }


def reconcile_voucher(voucher_id: int, note: str = "",
                      db_path: str | None = None) -> dict:
    """日后核齐：凭证关闭；主表断边在端点恢复后方可复活。"""
    note = (note or "").strip()
    with get_db_immediate(db_path) as conn:
        voucher = conn.execute(
            "SELECT * FROM dead_letter_vouchers WHERE id=?", (voucher_id,)
        ).fetchone()
        if not voucher:
            raise ServiceError("死信凭证不存在", 404)
        if voucher["status"] == "已核齐":
            raise ServiceError("凭证已核齐，不可重复操作", 409)
        revived_edge_id = None
        if voucher["edge_id"]:
            edge = conn.execute(
                "SELECT * FROM lineage_edges WHERE id=?", (voucher["edge_id"],)
            ).fetchone()
            if not edge:
                raise ServiceError("凭证对应的主表边已不存在，无法核齐", 409)
            broken, _ = _is_broken(conn, edge)
            if broken:
                raise ServiceError("端点节点仍停用或缺失，须先恢复节点再核齐", 409)
            current_fp = _edge_current_fingerprint(conn, edge)
            if current_fp != voucher["fingerprint"]:
                raise ServiceError("现行边指纹与凭证对不上，已拦住核齐", 409)
            conn.execute(
                "UPDATE lineage_edges SET status='草稿' WHERE id=?", (edge["id"],)
            )
            revived_edge_id = edge["id"]
        conn.execute(
            "UPDATE dead_letter_vouchers SET status='已核齐', "
            "reconciled_at=datetime('now'), note=? WHERE id=?",
            (note, voucher_id),
        )
        out = dict(conn.execute(
            "SELECT * FROM dead_letter_vouchers WHERE id=?", (voucher_id,)
        ).fetchone())
        out["revived_edge_id"] = revived_edge_id
        return out
