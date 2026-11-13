# 采购公告采集 API

传入**采购公告列表页 URL** 与**项目编号**，自动抓取该页指向的同站点原公告与更正公告，
从详情页提取发布时间、投标截止时间与取消采购信息，按发布时间合并后返回：
当前是否可投标、有效截止时间、项目状态以及全部来源链接。

## 业务规则

| 规则 | 实现位置 |
| --- | --- |
| 只采集列表页指向的**同站点**详情链接，外站链接、登录/导航链接自动忽略 | `app/parser.py::extract_detail_links` |
| 按**发布时间**从早到晚合并原公告与更正公告 | `app/merger.py::merge_notices` |
| 更正公告**未提及截止时间**时，沿用此前已生效的截止时间 | `app/merger.py` |
| 更正公告给出新截止时间（如延期）时，以新时间覆盖 | `app/merger.py` |
| 公告明确**取消采购** → 状态 `closed`、不可投标，不再返回有效截止时间（旧时间仅在公告明细中保留） | `app/merger.py` |
| **任一关联详情页**抓取/解析失败（404、超时、结构异常等）→ 状态 `pending` 待核实，`effective_deadline` 强制为 `null`，**绝不把旧时间当作最终期限** | `app/merger.py` + `app/collector.py` |
| 详情页收到 **429 限流**时按 `Retry-After`（delta-seconds 或 HTTP-date）有界重试：同一查询的所有并发详情**共享冷却截止时刻**，后续 429 只能延长冷却；总等待时长受 `max_rate_limit_wait` 限制 | `app/ratelimit.py` + `app/collector.py` |
| 等待**超过总时长上限**、`Retry-After` **无效**或重试仍失败 → 该来源标记待核实、清空有效期限并在来源/警告中列出；**无关项目的失败不改变本项目结论** | `app/collector.py` + `app/merger.py` |
| 发布时间缺失、缺少原公告、全部公告均无截止时间等信息链不完整情况，同样标记 `pending` | `app/merger.py` |

### 项目状态（`status`）

- `open`：可投标，`effective_deadline` 为合并后的有效截止时间；
- `closed`：采购已取消，`can_bid=false`，无有效截止时间；
- `pending`：待核实，存在关联页面失败或信息不完整，`effective_deadline=null`；
- `not_found`：列表页未发现与该项目编号相关的公告。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 入口、路由、浏览器演示页
│   ├── collector.py   # 采集编排：列表发现 → 并发抓取（含 429 有界恢复）→ 归属判定 → 合并
│   ├── ratelimit.py   # 有界限流恢复：Retry-After 解析、并发共享冷却与等待预算
│   ├── parser.py      # HTML 解析：同站链接发现、三层字段提取、取消识别
│   ├── merger.py      # 业务合并规则（时间继承、延期覆盖、取消、待核实）
│   └── models.py      # 请求/响应 Pydantic 模型
├── sample-site/       # 本地示例公告站点
│   ├── list.html      # 公告列表页（含外站/登录干扰链接，nginx 托管）
│   ├── detail/        # 3 个项目的原公告/更正/取消详情页
│   ├── ratelimited/   # 限流演示列表与 4 个项目的详情页（先限流后恢复/持续限流/无效头）
│   └── ratelimit_server.py # 纯标准库脚本化 429 服务器（离线可运行，也供测试使用）
├── nginx/default.conf # 示例站点 nginx 配置（缺失页面返回真实 404）
├── tests/             # 解析/合并/限流单测 + 本地服务器端到端测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 公告采集 API | http://localhost:8000 | Swagger 文档：http://localhost:8000/docs |
| 浏览器演示页 | http://localhost:8000/ | 填表即可发起采集 |
| 示例公告站点 | http://localhost:8080/list.html | nginx 托管的静态采购公告（404 失败场景） |
| 限流演示站点 | http://localhost:8081/ratelimited/list.html | 纯 Python 标准库脚本化 429（先限流后恢复/持续限流） |

停止：`docker compose down`。

### 离线调用限流演示站点（不使用 Docker）

限流服务器只依赖 Python 标准库，无需安装任何第三方包：

```bash
# 终端 A：启动脚本化 429 站点（默认 :8081）
python -I sample-site/ratelimit_server.py --host 127.0.0.1 --port 8081 \
  --root sample-site

# 终端 B：对限流列表发起采集（先限流后恢复的项目）
curl -X POST http://localhost:8000/api/v1/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url":"http://127.0.0.1:8081/ratelimited/list.html",
       "project_code":"GSB-2026-004","max_rate_limit_wait":10}'
```

## API 用法

### POST（推荐）

```bash
curl -X POST http://localhost:8000/api/v1/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url":"http://localhost:8080/list.html","project_code":"GSB-2026-001"}'
```

### GET（便于浏览器快速验证）

```bash
curl "http://localhost:8000/api/v1/collect?list_url=http://localhost:8080/list.html&project_code=GSB-2026-001"
```

请求参数：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `list_url` | 必填 | 采购公告列表页 URL |
| `project_code` | 必填 | 项目编号 |
| `timeout` | `10.0` | 单次 HTTP 请求超时（秒，1–60） |
| `max_rate_limit_wait` | `10.0` | **同一查询**为 429 冷却等待的总时长上限（秒，0–120），所有并发详情共享；超限即按待核实处理 |

响应关键字段：

```json
{
  "project_code": "GSB-2026-001",
  "status": "open",
  "can_bid": true,
  "effective_deadline": "2026-11-15T14:00:00",
  "cancelled": false,
  "notices": [ { "kind": "original", "published_at": "...", "deadline": "..." } ],
  "sources": [ { "role": "list", "url": "..." }, { "role": "correction", "url": "..." } ],
  "warnings": []
}
```

## 示例站点内置的三个演示项目

| 项目编号 | 场景 | 预期结果 |
| --- | --- | --- |
| `GSB-2026-001` | 原公告 → 澄清更正（**未提截止时间，沿用原时间**）→ 延期更正（**新时间覆盖**） | `open`，有效截止时间 `2026-11-15 14:00` |
| `GSB-2026-002` | 原公告 → 取消公告（预算收回） | `closed`，不可投标，无有效截止时间 |
| `GSB-2026-003` | 原公告 → 更正页**已下线（404）** | `pending` 待核实，`effective_deadline=null`，来源中列出失败链接 |

另外列表页还包含：外站链接、登录导航链接、其他项目公告（`GSB-2026-099`）、
无关项目的坏链接（`GSB-2025-888`），用于验证同站过滤、项目归属判定与失败隔离。

## 限流演示站点的四个项目（http://localhost:8081/ratelimited/list.html）

由 `sample-site/ratelimit_server.py`（纯标准库）脚本化返回 429，
每次查询独立维护请求计数，重启或重新发起即恢复初始“先限流”状态。

| 项目编号 | 限流行为 | 预期结果 |
| --- | --- | --- |
| `GSB-2026-004` | 更正页**首次 429**（`Retry-After: 1`，delta-seconds），随后恢复 | 等待共享冷却后重试成功，`open`，截止时间 `2026-11-19 15:00` |
| `GSB-2026-005` | 更正页**持续 429**，`Retry-After` 为 **HTTP-date** | 等待预算耗尽 → `pending`，`effective_deadline=null`，列出失败来源 |
| `GSB-2026-006` | 更正页持续 429 且 `Retry-After` **无法解析** | 不做无界等待，立即 `pending` 并注明无效 `Retry-After` |
| `GSB-2026-007` | 本项目详情正常；**无关项目** `GSB-2025-777` 持续 429 | 无关失败被隔离，本项目仍 `open`，无失败来源 |

### 有界限流恢复如何工作

1. 一次查询内的所有并发详情任务共享一个 `RateLimitCoordinator`（冷却截止时刻 + 等待预算）；
2. 任一详情收到 429，解析 `Retry-After`（秒数或 HTTP 日期）后设定/延长**共享冷却截止时刻**；
3. 尚未发出请求的任务在发请求前先等共享冷却结束——不会在站点限流期间继续打请求；
4. 后续 429 只能把冷却**向后延长**；全部任务的等待总时长不超过 `max_rate_limit_wait`；
5. 预算耗尽、`Retry-After` 无效或重试次数用尽 → 该详情计为失败，仅当它**属于本项目**时
   才把整体结论置为 `pending` 并清空有效期限，失败 URL 与原因进入 `sources`/`warnings`。

## 详情页解析策略（三层回退）

1. **结构化标记**：`data-field="published-at|deadline|cancelled|..."` 元素或
   `<meta property="article:published_time">`；
2. **标签匹配**：`发布时间 / 投标截止时间 / 响应文件递交截止时间 / 取消原因` 等
   中文标签，兼容 `th/td`、`dt/dd`、`li/p` 等布局，支持
   `2026-11-10 09:30`、`2026年11月15日 14时00分` 等常见写法；
3. **正文正则兜底**：在含“截止/递交/提交/受理”等时限语义的块级元素中识别日期，
   并排除“发布时间”句、资质业绩说明中的历史日期（如“近三年（2023年1月1日起）”）。

取消采购识别支持 `data-field="cancelled"` 标记、取消原因标签以及
“本项目取消采购 / 终止本次招标”等显式句式。

> 接入真实采购平台时，若页面结构不同，优先在详情页模板中补 `data-field` 标记，
> 或在 `app/parser.py` 顶部的标签/关键词元组中登记该平台的字段叫法。

## 本地开发与测试（不使用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 终端 A：托管示例公告页
cd sample-site && python -m http.server 8080
# 终端 B：启动 API
uvicorn app.main:app --reload --port 8000

# 测试（自带静态服务器，无需外部依赖）
pytest -q
```

## 设计说明

- 列表页抓取失败直接返回 `502`；详情页失败不影响整体响应，而是按业务规则沉淀为
  `pending` + 失败来源链接，便于投标专员人工核实。
- 关联归属判定结合详情页项目编号与列表行上下文（含编号列），
  即使详情页下线（只能看到列表行）也能正确判定失败是否与目标项目相关；
  无关项目的坏链接/限流不会污染查询结果。
- 详情页并发抓取（上限 5），统一超时、自定义 UA、跟随重定向。
- 429 恢复有界：并发详情共享同一冷却截止时刻与等待预算（`time.monotonic` 计时，
  不受系统时钟回调影响）；冷却只延长不缩短，等待总时长封顶，杜绝无限重试风暴。
