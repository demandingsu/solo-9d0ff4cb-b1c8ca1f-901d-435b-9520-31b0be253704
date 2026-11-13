"""采集编排：列表页发现 → 详情页抓取（含有界 429 限流恢复）→ 字段解析 → 业务合并。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException

from app.merger import MergeInput, merge_notices
from app.models import (
    FetchRequest,
    FetchResponse,
    NoticeKind,
    NoticeRecord,
    ProjectStatus,
    SourceLink,
)
from app.parser import ParseError, extract_detail_links, parse_detail
from app.ratelimit import (
    InvalidRetryAfter,
    RateLimitBudgetExceeded,
    RateLimitCoordinator,
    parse_retry_after,
)

_MAX_CONCURRENCY = 5
# 单个详情在 429 恢复之外的最大重试次数（防止 Retry-After=0 时形成忙等死循环）
_MAX_429_RETRIES = 3
_USER_AGENT = "TenderCollector/1.0 (+local procurement announcement API)"
# 项目编号匹配时容忍大小写/空格/分隔符差异，如 “GSB-2026-001” 与 “GSB 2026 001”
_CODE_NORM = str.maketrans({" ": "", "　": "", "_": "", "/": "", "\\": "", "-": ""})


def _normalize_code(code: str) -> str:
    return code.strip().lower().translate(_CODE_NORM)


def _code_matches(page_code: str | None, requested: str, title: str, row_context: str) -> bool:
    target = _normalize_code(requested)
    if page_code and _normalize_code(page_code) == target:
        return True
    # 详情页没有结构化编号时，退化为标题/列表行上下文包含匹配
    haystack = _normalize_code(f"{title} {row_context}")
    return target in haystack


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url)
    # 4xx/5xx（如详情页下线返回 404）一律视为抓取失败
    response.raise_for_status()
    return response.text


@dataclass
class DetailOutcome:
    """单个详情页的抓取结果：成功带 html，失败带原因（用于待核实时标注来源）。"""

    url: str
    link_text: str
    context: str
    html: str = ""
    error: str | None = None


async def _fetch_detail_with_recovery(
    client: httpx.AsyncClient,
    url: str,
    link_text: str,
    context: str,
    coordinator: RateLimitCoordinator,
) -> DetailOutcome:
    """抓取单个详情页，遇到 429 时按共享冷却与有界等待重试。

    流程：每次真正发请求前先等待共享冷却（若有）；收到 429 则解析 Retry-After
    并延长共享冷却后重试。等待预算耗尽、Retry-After 无效或重试仍失败时，
    返回带 ``error`` 的失败结果，由调用方按项目归属决定是否计入待核实。
    """
    attempts = 0
    while True:
        try:
            # 尚未发出的请求必须等共享冷却结束（预算耗尽会在此快速失败）
            await coordinator.wait_for_cooldown()
            response = await client.get(url)
        except RateLimitBudgetExceeded:
            return DetailOutcome(url, link_text, context, error="限流等待超过总时长上限")
        except httpx.HTTPError as exc:
            # 连接错误/超时等网络层失败不做限流重试
            return DetailOutcome(url, link_text, context, error=f"请求失败（{exc.__class__.__name__}）")

        if response.status_code != 429:
            try:
                response.raise_for_status()
            except httpx.HTTPError as exc:
                return DetailOutcome(url, link_text, context, error=f"HTTP {response.status_code}（{exc.__class__.__name__}）")
            return DetailOutcome(url, link_text, context, html=response.text)

        # 收到 429：解析 Retry-After（秒数或 HTTP 日期），无效即失败
        try:
            wait_seconds = parse_retry_after(response.headers.get("Retry-After"))
        except InvalidRetryAfter as exc:
            return DetailOutcome(url, link_text, context, error=f"无效 Retry-After：{exc}")

        attempts += 1
        if attempts > _MAX_429_RETRIES:
            return DetailOutcome(url, link_text, context, error="429 重试次数已用尽")
        try:
            # 后续 429 可延长共享冷却；预算不足以覆盖等待时直接失败
            await coordinator.extend_cooldown(wait_seconds)
        except RateLimitBudgetExceeded:
            return DetailOutcome(url, link_text, context, error="限流等待超过总时长上限")
        except InvalidRetryAfter as exc:
            return DetailOutcome(url, link_text, context, error=f"无效 Retry-After：{exc}")


async def collect(req: FetchRequest) -> FetchResponse:
    list_url = str(req.list_url)
    timeout = httpx.Timeout(req.timeout)
    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": _USER_AGENT}, follow_redirects=True
    ) as client:
        # 1) 抓列表页
        try:
            list_html = await _fetch(client, list_url)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"列表页抓取失败：{list_url}（{exc.__class__.__name__}）",
            ) from exc

        # 2) 发现同站点详情链接（URL, 链接文本, 行上下文）
        candidates = extract_detail_links(list_html, list_url)
        if not candidates:
            return _empty_response(req, warnings=["列表页未发现任何同站点详情链接"])

        target_code = _normalize_code(req.project_code)
        semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
        # 同一查询的所有并发详情共享冷却截止时刻与等待预算
        coordinator = RateLimitCoordinator(req.max_rate_limit_wait)

        async def fetch_one(url: str, link_text: str, context: str) -> DetailOutcome:
            async with semaphore:
                return await _fetch_detail_with_recovery(
                    client, url, link_text, context, coordinator
                )

        # 3) 并发抓取候选详情页（429 在内部按共享冷却有界重试）
        results = await asyncio.gather(
            *(fetch_one(u, t, c) for u, t, c in candidates)
        )

        notices: list[NoticeRecord] = []
        failures: list[str] = []
        failure_reasons: dict[str, str] = {}

        for outcome in results:
            url, html, link_text, context = (
                outcome.url,
                outcome.html,
                outcome.link_text,
                outcome.context,
            )
            # 列表行上下文包含目标项目编号 → 该链接属于本项目（即使详情页抓不回来）
            row_mentions_project = target_code in _normalize_code(context)

            if not html:
                if row_mentions_project:
                    failures.append(url)
                    if outcome.error:
                        failure_reasons[url] = outcome.error
                continue  # 无关项目的失败不改变本项目结论
            try:
                data = parse_detail(html, url)
            except ParseError as exc:
                if row_mentions_project:
                    failures.append(url)
                    failure_reasons[url] = f"页面解析失败：{exc.__class__.__name__}"
                continue

            if not _code_matches(data.project_code, req.project_code, data.title, context):
                continue  # 同站点但属于其他项目，忽略

            kind = {
                "original": NoticeKind.ORIGINAL,
                "correction": NoticeKind.CORRECTION,
                "cancellation": NoticeKind.CANCELLATION,
            }[data.kind_hint]
            notices.append(
                NoticeRecord(
                    url=url,
                    kind=kind,
                    title=data.title,
                    published_at=data.published_at,
                    deadline=data.deadline,
                    cancelled=data.cancelled,
                    cancellation_reason=data.cancellation_reason,
                )
            )
            # 缺发布时间在 merger 中按待核实处理。

    # 4) 业务合并（只有本项目相关详情取齐才按原规则合并；失败由 merger 判 pending）
    unique_failures = sorted(set(failures))
    merge_result = merge_notices(
        MergeInput(notices=notices, failures=unique_failures)
    )

    # 限流/重试等失败原因补充进 warnings，便于投标专员核实
    for failed_url in unique_failures:
        reason = failure_reasons.get(failed_url)
        if reason:
            merge_result.warnings.append(f"来源 {failed_url}：{reason}")

    # 5) 组装来源链接
    sources: list[SourceLink] = [
        SourceLink(role="list", url=list_url, title="采购公告列表页")
    ]
    role_map = {
        NoticeKind.ORIGINAL: "original",
        NoticeKind.CORRECTION: "correction",
        NoticeKind.CANCELLATION: "cancellation",
    }
    for notice in merge_result.ordered_notices:
        sources.append(
            SourceLink(role=role_map[notice.kind], url=notice.url, title=notice.title)
        )
    for failed_url in unique_failures:
        reason = failure_reasons.get(failed_url)
        title = f"抓取失败-待核实（{reason}）" if reason else "抓取失败-待核实"
        sources.append(SourceLink(role="failed", url=failed_url, title=title))

    return FetchResponse(
        project_code=req.project_code,
        list_url=req.list_url,
        status=merge_result.status,
        can_bid=merge_result.can_bid,
        effective_deadline=merge_result.effective_deadline,
        cancelled=merge_result.cancelled,
        notices=merge_result.ordered_notices,
        sources=sources,
        warnings=merge_result.warnings,
        fetched_at=datetime.now(timezone.utc),
    )


def _empty_response(req: FetchRequest, warnings: list[str]) -> FetchResponse:
    return FetchResponse(
        project_code=req.project_code,
        list_url=req.list_url,
        status=ProjectStatus.NOT_FOUND,
        can_bid=False,
        effective_deadline=None,
        cancelled=False,
        notices=[],
        sources=[
            SourceLink(role="list", url=str(req.list_url), title="采购公告列表页")
        ],
        warnings=warnings,
        fetched_at=datetime.now(timezone.utc),
    )
