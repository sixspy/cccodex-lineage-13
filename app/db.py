import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS lineage_nodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_code TEXT NOT NULL UNIQUE,
  table_name TEXT NOT NULL,
  layer TEXT NOT NULL CHECK (layer IN ('ODS','DWD','DWS','ADS')),
  owner_team TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('登记','在用','停用')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  edge_code TEXT NOT NULL UNIQUE,
  upstream_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  downstream_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  transform_kind TEXT NOT NULL DEFAULT '直抽',
  status TEXT NOT NULL CHECK (status IN ('草稿','已发布','已作废','已死信')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(upstream_id, downstream_id)
);

CREATE TABLE IF NOT EXISTS impact_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_code TEXT NOT NULL UNIQUE,
  root_node_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  status TEXT NOT NULL CHECK (status IN ('待执行','执行中','成功','失败')),
  affected_count INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS impact_hits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES impact_jobs(id),
  node_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  hop INTEGER NOT NULL DEFAULT 0,
  UNIQUE(job_id, node_id)
);

CREATE TABLE IF NOT EXISTS notify_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES impact_jobs(id),
  node_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  channel TEXT NOT NULL DEFAULT '站内',
  status TEXT NOT NULL CHECK (status IN ('待发','已发','失败')),
  body TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS coverage_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  summary TEXT NOT NULL,
  node_count INTEGER NOT NULL,
  edge_count INTEGER NOT NULL,
  orphan_count INTEGER NOT NULL,
  uncovered_edges INTEGER NOT NULL,
  dead_letter_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 旁路账册：断边死信凭证（命运只此一种，独立队列吞没模式不实现）
CREATE TABLE IF NOT EXISTS dead_letter_vouchers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  voucher_code TEXT NOT NULL UNIQUE,
  edge_id INTEGER,
  edge_code TEXT NOT NULL,
  upstream_ref TEXT NOT NULL,
  downstream_ref TEXT NOT NULL,
  transform_kind TEXT NOT NULL DEFAULT '直抽',
  fingerprint TEXT NOT NULL,
  short_fingerprint TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL CHECK (source IN ('人工送死信','扫描断边')),
  status TEXT NOT NULL CHECK (status IN ('待核齐','已核齐')),
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  reconciled_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
-- 串行闸：同一待核齐指纹只许一张凭证，同一边只许一张待核齐凭证
CREATE UNIQUE INDEX IF NOT EXISTS idx_dlv_fingerprint_open
  ON dead_letter_vouchers(fingerprint) WHERE status='待核齐';
CREATE UNIQUE INDEX IF NOT EXISTS idx_dlv_edge_open
  ON dead_letter_vouchers(edge_id) WHERE status='待核齐' AND edge_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS lab_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  label TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_db_immediate(db_path: str | None = None):
    """送死信用的串行闸：进入即取库级写锁，同库并发送死信在此排队。"""
    conn = connect(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_schema(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
