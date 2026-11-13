"""端到端测试：限流示例站点（脚本化 429）→ FastAPI 完整采集链路。

覆盖需求场景：
* 先限流后恢复：详情首次 429（Retry-After 秒数），冷却后重试成功并正常合并；
* 持续限流（Retry-After 为 HTTP 日期）：有界等待预算耗尽 → pending、清空截止时间、列出失败来源；
* 持续限流且 Retry-After 无效：立即失败 → pending；
* 无关项目持续限流：不改变本项目（详情全部取齐）的结论。
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = REPO_ROOT / "sample-site"
_SERVER_PATH = SAMPLE_DIR / "ratelimit_server.py"

# sample-site 目录名含连字符，不能作为包导入，按文件路径加载；
# 需先注册到 sys.modules，dataclasses 才能解析模块内的类型引用
_spec = importlib.util.spec_from_file_location("ratelimit_server", _SERVER_PATH)
ratelimit_server = importlib.util.module_from_spec(_spec)
sys.modules["ratelimit_server"] = ratelimit_server
_spec.loader.exec_module(ratelimit_server)  # type: ignore[union-attr]


@pytest.fixture(scope="module")
def rate_limited_server():
    server = ratelimit_server.make_server(SAMPLE_DIR)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}/ratelimited/list.html", server
    server.shutdown()
    server.server_close()


@pytest.fixture()
def client(rate_limited_server):
    # 每个用例前清空限流计数，保证“首次请求 429”的初始状态
    _, server = rate_limited_server
    ratelimit_server.reset_counters(server)
    with TestClient(app) as c:
        yield c


def _collect(client, list_url, code, max_wait=10.0, timeout=5.0):
    resp = client.post(
        "/api/v1/collect",
        json={
            "list_url": list_url,
            "project_code": code,
            "timeout": timeout,
            "max_rate_limit_wait": max_wait,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_recovers_after_first_429_then_merges_correction(client, rate_limited_server):
    list_url, _ = rate_limited_server
    data = _collect(client, list_url, "GSB-2026-004", max_wait=10.0)

    # 冷却后重试成功 → 按原规则合并，延期后的截止时间生效
    assert data["status"] == "open"
    assert data["can_bid"] is True
    assert data["effective_deadline"].startswith("2026-11-19T15:00:00")
    assert [n["kind"] for n in data["notices"]] == ["original", "correction"]
    assert not any(s["role"] == "failed" for s in data["sources"])


def test_persistent_429_with_http_date_exhausts_budget_and_pending(client, rate_limited_server):
    list_url, _ = rate_limited_server
    data = _collect(client, list_url, "GSB-2026-005", max_wait=2.0)

    assert data["status"] == "pending"
    assert data["can_bid"] is False
    # 关键：待核实时绝不返回原公告的旧截止时间
    assert data["effective_deadline"] is None
    failed = [s for s in data["sources"] if s["role"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["url"].endswith("/ratelimited/g005-corr.html")
    assert any("限流" in w for w in data["warnings"])


def test_invalid_retry_after_marks_pending_without_waiting(client, rate_limited_server):
    list_url, _ = rate_limited_server
    data = _collect(client, list_url, "GSB-2026-006", max_wait=10.0)

    assert data["status"] == "pending"
    assert data["effective_deadline"] is None
    failed = [s for s in data["sources"] if s["role"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["url"].endswith("/ratelimited/g006-corr.html")
    # 无效 Retry-After 应被明确记录，而不是静默吞掉
    assert any("Retry-After" in w for w in data["warnings"])


def test_unrelated_persistent_429_does_not_change_project_conclusion(client, rate_limited_server):
    list_url, _ = rate_limited_server
    # GSB-2026-007 自身只有一个始终正常的原公告；持续限流的是无关项目 GSB-2025-777
    data = _collect(client, list_url, "GSB-2026-007", max_wait=2.0)

    assert data["status"] == "open"
    assert data["can_bid"] is True
    assert data["effective_deadline"].startswith("2026-11-26T14:00:00")
    # 无关项目的失败链接不得出现在本项目来源中
    assert not any("unrelated-throttled" in s["url"] for s in data["sources"])
    assert not any(s["role"] == "failed" for s in data["sources"])


def test_get_endpoint_accepts_rate_limit_budget(client, rate_limited_server):
    list_url, _ = rate_limited_server
    resp = client.get(
        "/api/v1/collect",
        params={
            "list_url": list_url,
            "project_code": "GSB-2026-004",
            "max_rate_limit_wait": 10.0,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "open"


def test_concurrent_details_share_single_cooldown():
    """两个并发详情：首个 429 设定的共享冷却应同时拦住另一个尚未发出的请求。

    若各任务独立等待，总耗时接近两次 Retry-After 之和；共享冷却时总耗时只约一次。
    用独立规则集与列表页，避免污染模块级服务器的限流计数。
    """
    rules = (
        # 仅首次 429（1 秒），随后恢复
        ratelimit_server.RateLimitRule("/ratelimited/g004-corr.html", fail_times=1, retry_after_value=1.0),
        ratelimit_server.RateLimitRule("/ratelimited/g004-original.html", fail_times=1, retry_after_value=1.0),
    )
    server = ratelimit_server.make_server(SAMPLE_DIR, rules=rules)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        with TestClient(app) as c:
            import time

            started = time.monotonic()
            resp = c.post(
                "/api/v1/collect",
                json={
                    "list_url": f"http://127.0.0.1:{port}/ratelimited/list.html",
                    "project_code": "GSB-2026-004",
                    "timeout": 5,
                    "max_rate_limit_wait": 10.0,
                },
            )
            elapsed = time.monotonic() - started
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # 两个详情都经一次共享冷却后恢复，合并成功
        assert data["status"] == "open"
        assert data["effective_deadline"].startswith("2026-11-19T15:00:00")
        # 共享一次 1s 冷却：明显小于两次独立等待（>=2s），并留足调度余量
        assert elapsed < 1.9, f"并发详情未共享冷却，总耗时 {elapsed:.2f}s"
    finally:
        server.shutdown()
        server.server_close()


def test_repeated_429_exhausts_retry_count_and_marks_pending():
    """持续返回带合法 Retry-After 的 429，但预算足够长：重试次数用尽 → pending。"""
    rules = (
        # 限流 4 次（超过单详情 3 次重试上限），每次仅需等待 0 秒
        ratelimit_server.RateLimitRule("/ratelimited/g004-corr.html", fail_times=4, retry_after_value=0.0),
    )
    server = ratelimit_server.make_server(SAMPLE_DIR, rules=rules)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/collect",
                json={
                    "list_url": f"http://127.0.0.1:{port}/ratelimited/list.html",
                    "project_code": "GSB-2026-004",
                    "timeout": 5,
                    "max_rate_limit_wait": 10.0,
                },
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "pending"
        assert data["effective_deadline"] is None
        failed = [s for s in data["sources"] if s["role"] == "failed"]
        assert len(failed) == 1 and failed[0]["url"].endswith("g004-corr.html")
        assert any("重试次数" in w for w in data["warnings"])
    finally:
        server.shutdown()
        server.server_close()
