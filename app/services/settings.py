"""台参数：影响深度与通知渠道。"""
from __future__ import annotations

from app.db import get_db
from app.services.deadletter import DEAD_LETTER_MODE
from app.services.errors import ServiceError

# 断边处理命运只兑现一种，模式键只读
DEAD_LETTER_MODE_KEY = "dead_letter_mode"


def list_settings(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT key, value, label FROM lab_settings ORDER BY key"
        ).fetchall()
        return [dict(r) for r in rows]


def update_setting(key: str, value: str, db_path: str | None = None) -> dict:
    key = (key or "").strip()
    value = (value or "").strip()
    if not key or not value:
        raise ServiceError("参数键与值不能为空")
    if key == DEAD_LETTER_MODE_KEY:
        raise ServiceError(
            f"断边命运已固定为「{DEAD_LETTER_MODE}」，独立队列吞没模式不可并存、不可切换",
            409,
        )
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM lab_settings WHERE key=?", (key,)
        ).fetchone()
        if not row:
            raise ServiceError("参数不存在", 404)
        conn.execute(
            "UPDATE lab_settings SET value=? WHERE key=?", (value, key)
        )
        return dict(
            conn.execute(
                "SELECT key, value, label FROM lab_settings WHERE key=?", (key,)
            ).fetchone()
        )
