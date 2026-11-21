"""HTTP 层验收：登录态、中文错误、死信接口一次落库。不跑无头脚本。"""
import httpx

from app.main import create_app


def _client():
    transport = httpx.WSGITransport(app=create_app())
    return httpx.Client(transport=transport, base_url="http://lineage")


def _login(client):
    r = client.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200


def test_dead_letter_requires_login():
    with _client() as client:
        r = client.get("/api/dead-letters")
        assert r.status_code == 401
        assert r.json()["error"] == "未登录"


def test_dead_letter_queue_and_mode():
    with _client() as client:
        _login(client)
        r = client.get("/api/dead-letters")
        assert r.status_code == 200
        data = r.json()
        assert data["mode"] == "旁路账册死信凭证"
        assert data["forbidden_mode"] == "独立队列吞没"
        assert data["isolated_count"] == 2
        sources = {v["source"] for v in data["items"] if v["status"] == "待核齐"}
        assert sources == {"人工送死信", "扫描断边"}


def test_scan_endpoint_returns_chinese_block_reason():
    with _client() as client:
        _login(client)
        r = client.post("/api/dead-letters/scan-ingest", json={
            "items": [{
                "edge_code": "E-01",
                "upstream_code": "N-ODS-ORD",
                "downstream_code": "N-DWD-ORD",
                "transform_kind": "篡改",
            }]
        })
        assert r.status_code == 200
        report = r.json()["report"]
        assert report["blocked_count"] == 1
        reason = report["blocked"][0]["reason"]
        assert "指纹" in reason and "拦" in reason


def test_send_healthy_edge_dead_letter_returns_chinese_error():
    with _client() as client:
        _login(client)
        edges = client.get("/api/edges").json()["items"]
        healthy = next(e for e in edges if e["status"] == "已发布")
        r = client.post(f"/api/edges/{healthy['id']}/dead-letter", json={"reason": ""})
        assert r.status_code == 409
        assert "不是断边" in r.json()["error"]


def test_impact_jobs_carry_isolated_count():
    with _client() as client:
        _login(client)
        nodes = client.get("/api/nodes").json()["items"]
        root = next(n for n in nodes if n["node_code"] == "N-DWD-ORD")
        client.post("/api/impact/start", json={"root_node_id": root["id"]})
        jobs = client.get("/api/impact/jobs").json()["items"]
        assert jobs[0]["isolated_count"] == 2


def test_mode_change_rejected_over_http():
    with _client() as client:
        _login(client)
        r = client.post("/api/settings", json={
            "key": "dead_letter_mode",
            "value": "独立队列吞没",
        })
        assert r.status_code == 409
        assert "不可并存" in r.json()["error"]
