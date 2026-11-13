"""示例站点限流服务器（仅使用 Python 标准库，离线可运行）。

在普通静态文件服务之外，对配置的详情页路径模拟 429 限流：

* 每个受控路径维护一个请求计数器，前 ``fail_times`` 次请求返回
  ``429 + Retry-After``（delta-seconds 或 HTTP-date 两种形式都有演示），
  之后恢复为正常页面 —— 演示“先限流后恢复”；
* ``fail_times`` 设为一个很大的数即“持续限流”，用于演练等待预算耗尽 → 待核实；
* 受控路径恢复后与普通静态页面行为一致，未受控路径完全不受影响。

同时供 pytest 端到端测试以编程方式启动（:class:`RateLimitRule` /
:func:`build_rate_limited_handler` / :func:`reset_counters`）。
"""

from __future__ import annotations

import argparse
import functools
import http.server
import threading
from dataclasses import dataclass
from email.utils import formatdate
from pathlib import Path
from time import monotonic, time

# 受控详情路径规则：路径 → (前 N 次返回 429, Retry-After 形式, Retry-After 值)
# date 形式的值为相对当前时刻的秒数，响应时格式化为 HTTP-date
@dataclass(frozen=True)
class RateLimitRule:
    path: str
    fail_times: int
    retry_after_kind: str = "seconds"  # "seconds" → delta-seconds；"date" → HTTP-date
    retry_after_value: float = 1.0


DEFAULT_RULES: tuple[RateLimitRule, ...] = (
    # 场景一：先限流后恢复（第 1 次 429，Retry-After: 1，随后恢复）
    RateLimitRule("/ratelimited/g004-corr.html", fail_times=1, retry_after_value=1.0),
    # 场景二：持续限流，Retry-After 使用 HTTP-date 形式
    RateLimitRule(
        "/ratelimited/g005-corr.html",
        fail_times=10_000,
        retry_after_kind="date",
        retry_after_value=1.0,
    ),
    # 场景三：持续限流且 Retry-After 无效
    RateLimitRule(
        "/ratelimited/g006-corr.html",
        fail_times=10_000,
        retry_after_kind="invalid",
        retry_after_value=1.0,
    ),
    # 场景四：无关项目持续限流，本项目详情正常 → 不影响本项目结论
    RateLimitRule(
        "/ratelimited/unrelated-throttled.html",
        fail_times=10_000,
        retry_after_value=1.0,
    ),
)


class _RateLimitState:
    """线程安全的每路径请求计数。"""

    def __init__(self, rules: tuple[RateLimitRule, ...]):
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {rule.path: 0 for rule in rules}
        self.rules = {rule.path: rule for rule in rules}

    def consume(self, path: str) -> RateLimitRule | None:
        """登记一次请求；若该路径仍在限流窗口内则返回对应规则，否则返回 None。"""
        with self._lock:
            rule = self.rules.get(path)
            if rule is None:
                return None
            current = self._counts[path]
            self._counts[path] = current + 1
            return rule if current < rule.fail_times else None

    def reset(self) -> None:
        with self._lock:
            for path in self._counts:
                self._counts[path] = 0


def reset_counters(server: http.server.ThreadingHTTPServer) -> None:
    """清空限流计数，供测试之间恢复初始“先限流”状态。"""
    server.rate_limit_state.reset()  # type: ignore[attr-defined]


def build_rate_limited_handler(
    web_root: Path, rules: tuple[RateLimitRule, ...] = DEFAULT_RULES
) -> type[http.server.BaseHTTPRequestHandler]:
    """构造绑定静态根目录与限流规则的请求处理器类。"""
    state = _RateLimitState(rules)

    class _RateLimitedHandler(http.server.SimpleHTTPRequestHandler):
        rate_limit_state = state

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(web_root), **kwargs)

        def log_message(self, *args):  # 静默访问日志，保持测试/演示输出干净
            pass

        def do_GET(self):  # noqa: N802 - http.server 约定命名
            rule = state.consume(self.path.split("?", 1)[0])
            if rule is not None:
                self._send_rate_limited(rule)
                return
            super().do_GET()

        def _send_rate_limited(self, rule: RateLimitRule) -> None:
            body = (
                "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<title>429 请求过于频繁</title></head>"
                "<body><h1>429 Too Many Requests</h1>"
                "<p>站点短时限流，请按 Retry-After 指示稍后重试（演示场景）。</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(429)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if rule.retry_after_kind == "date":
                # HTTP-date 形式，如 Wed, 21 Oct 2026 07:28:00 GMT
                self.send_header("Retry-After", formatdate(time() + rule.retry_after_value, usegmt=True))
            elif rule.retry_after_kind == "invalid":
                self.send_header("Retry-After", "not-a-valid-retry-after")
            else:
                self.send_header("Retry-After", str(int(rule.retry_after_value)))
            self.end_headers()
            self.wfile.write(body)

    return _RateLimitedHandler


def make_server(
    web_root: Path,
    host: str = "127.0.0.1",
    port: int = 0,
    rules: tuple[RateLimitRule, ...] = DEFAULT_RULES,
) -> http.server.ThreadingHTTPServer:
    handler = build_rate_limited_handler(web_root, rules)
    server = http.server.ThreadingHTTPServer((host, port), handler)
    server.rate_limit_state = handler.rate_limit_state  # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="示例站点限流服务器（标准库实现）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent),
        help="静态文件根目录（默认为 sample-site，使 /ratelimited/... 路径与限流规则一致）",
    )
    args = parser.parse_args()

    web_root = Path(args.root).resolve()
    server = make_server(web_root, args.host, args.port)
    actual_port = server.server_address[1]
    print(
        f"限流示例站点已启动：http://{args.host}:{actual_port}/ratelimited/list.html",
        flush=True,
    )
    print(f"静态根目录：{web_root}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - 手动停止
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
