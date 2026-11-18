# 信贷数仓与受控智能问数学习项目

这是依据简历中的两段项目经历实现的可运行本地项目。它使用合成信贷数据，把离线数仓、实时状态、指标治理、LangGraph 问数、SQLGlot 安全、权限、审计、短期记忆和依赖降级连成一个闭环。项目不接触真实客户数据，也不把本地样例描述成生产部署。

## 快速运行

需要 Python 3.12 和 Docker Desktop。首次启动会建立 `.venv`、安装依赖、生成 2025-01 至 2026-06 的 18 个月末合成快照（覆盖认证目录中的全部指标，并保留 2026-03-01/02 两日样例）、重放实时事件，启动本机 Elasticsearch，使用本地 embedding 缓存发布认证知识后启动服务：

```bash
chmod +x scripts/start_demo.sh
./scripts/start_demo.sh
```

浏览器访问 <http://127.0.0.1:8000>。不要直接打开 `credit/web/index.html`；`file://` 页面没有正在运行的 API。

演示身份由服务端 Bearer token 映射：`demo-analyst`、`demo-operator`、`demo-visitor`，以及只读运行摘要的 `demo-platform`。这些凭证只在显式 `CREDIT_DEMO_MODE=1` 时可用。生产接入必须替换为 B 端 SSO/JWT 身份解析。

### 可选 DeepSeek 意图识别

系统默认使用本地确定性路由，不调用外部模型。可选启用 DeepSeek 后，用户问句会发送到 DeepSeek 官方 Chat Completions API，仅让模型建议 `catalog`、`definition`、`field`、`schema`、`lineage`、`calculation`、`query`、`exploration`、`other` 这些白名单意图；模型不能回答业务问题、决定权限、调用工具或生成 SQL。`calculation` 仅使用用户明确给出的分子、分母，按认证比率公式本地演算，不读数仓；`query` 才是认证数仓数值查询；`other` 明确拒答范围外问题。对于已登记的未认证候选指标，Agent 可根据指标和日期推断数值探索意图，不要求用户输入内部“探索试算”口令；只有候选目录、服务端身份权限和沙箱白名单都通过才会执行，并始终标注 `NON_CERTIFIED`。意图模型建议不授予权限；未知指标不会自动猜表、字段或生成 SQL。

在项目根目录复制 `.env.example` 为 `.env`（`.env` 已加入 Git 忽略），设置：

```dotenv
CREDIT_INTENT_ROUTER=deepseek
DEEPSEEK_API_KEY=粘贴你的_API_Key
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_TIMEOUT_SECONDS=4
```

启动脚本会自动读取根目录 `.env`。不要把 Key 写入源码、README、命令行参数或提交到 Git；如果问句中含有客户信息、账号、证件号等敏感内容，不要启用外发模式，直到完成脱敏与数据出境/供应商合规审查。`/healthz` 只显示路由器模式，不回显密钥。没有配置 `CREDIT_INTENT_ROUTER=deepseek` 和 `DEEPSEEK_API_KEY` 时，外部调用保持关闭。

本地安全验证可运行 `.venv/bin/python -m pytest tests/test_intent_router.py -q`；这些测试模拟 API，不会访问 DeepSeek，也不会产生费用。

意图路由开发集评测使用完全合成的问题，可将同一组问句对比本地规则与 DeepSeek。在线模式会实际调用 DeepSeek，可能产生费用；显式 `--online` 是必需选项。结果报告只保留案例 ID、标签、意图、状态和延迟，不写入原始问题：

```bash
set -a
source .env
set +a
.venv/bin/python -m credit.intent_evaluation --online --output var/intent-routing-evaluation.json
```

2026-10-22 的 29 条合成开发集全量回归：本地确定性路由 29/29；DeepSeek 在 28 条成功返回的样例上 28/28，另有 1 条“仅输入指标名称”未得到有效分类并回退本地规则，最终有效路由 29/29。单独重试该题 3 次，有 2 次返回 `other`、1 次再次回退，说明外部分类仍有间歇性失败，不能省略兜底。结果保存在 `/private/tmp/credit-intent-online-2026-10-22.json`。这些数据仅用于开发回归，不代表真实业务准确率或生产 SLA。

## 可验证链路

```text
bundle.json + _SUCCESS.json
  -> ODS 原始 JSON + 批次审计
  -> DWD 申请/贷款快照/还款 + SCD2 客户维度
  -> DWS 日粒度汇总 + 六项独立质量对账
  -> ADS FINAL 发布
                         ┌-> 字段/口径/血缘 + 引用
FastAPI -> LangGraph -> 权限 -> 认证工具
                         └-> SQLGlot 精确模板 -> 只读 SQLite -> 审计

LoanEvent -> 去重/版本控制 -> REALTIME ADS -> 与 FINAL 对账
```

系统内置 8 个认证指标和 11 个认证字段。FINAL 只在来源、DWD、DWS 三方对账通过后发布；REALTIME 保留独立状态，只用于暂态监控。余额类时点指标跨日查询必须包含业务日期，避免把每日余额累加为期间总额。

自然语言不会直接成为可执行 SQL。Agent 先识别认证指标和显式日期，再生成固定模板；SQLGlot 对候选 AST 与模板做结构等价检查，数据库使用只读连接、查询进度超时和行数上限。权限来自服务端身份，查询体、会话状态和自然语言都不能授予角色或数据域。

短期记忆只保存任务摘要：指标、日期、维度、数据状态、引用 ID 和 Trace ID。它不保存原始查询结果、SQL、身份或权限。Redis 失败时本轮降级为无状态并提示补齐上下文；审计属于必需能力，审计写入失败会拒绝发布结果。

## API 示例

问答页与新的异步任务 API 使用 `POST /v1/question-tasks`：提交后立即返回 `request_id`，通过 GET 查询状态，完成后返回结果；中断或结果缓存过期时，可在有效期内以同一身份 POST `/resume` 并重新提交问题。DELETE 会取消任务并清除内存结果。

调用方可在提交时携带 `Idempotency-Key`（1–128 位字母、数字、点、下划线、冒号或连字符）。同一用户用同一键重试完全相同的问题与 `session_id`，会得到原 `request_id` 和当前任务状态；同一键用于不同请求返回 409。键在 checkpoint 过期后可重新使用。不提供键时，每次提交都会创建新任务；页面目前未生成幂等键。服务端只保存键哈希及带随机盐的问题摘要，不保存原问题。

GET 响应中的 `intent_progress` 会逐个记录各并行意图的状态（如 `queued`、`running`、`ok`、`unavailable`、`needs_clarification`）；页面会显示“指标口径处理中 / 数据血缘已完成”等实时进度。它只记录意图和状态，不持久化问题内容或分支结果；SQLite 写进度失败时只影响进度展示，不应中断问答。

问句路由支持将“是多少／多少、口径、上游表”拆成数值查询、口径解释、数据血缘三个分项。当前逾期率的认证取数维度为业务日期与产品；“各渠道”会把数值分项标为暂不可用，同时保留能确认的口径和血缘。“上月”还需明确月末时点还是每日序列。本地合成数据覆盖 2025-01 至 2026-06 的月末快照，并保留 2026-03-01/02；不是逐日完整数据。Vintage 使用单独的合成放款批次快照，不代表生产数据源或认证血缘。

### 会话追问与上下文边界

带相同 `session_id` 的“那按产品呢”“那实时呢”“那口径呢”等明确短追问，可沿用当前任务中必要的指标/日期/维度，再应用本轮明确指定的新条件；身份与指标权限仍在每次工具调用重新校验。显式切换到新指标时，会重置上一轮的日期、维度和 REALTIME/FINAL 状态；如果新指标查询缺日期，会先要求补充日期。相对日期、同时要求 REALTIME 与 FINAL、或不明确的“那呢”不会自动复用旧值，会要求澄清。拒答后会清除旧任务上下文，避免下一条省略主语的问题意外续查旧指标。

```bash
curl -i -X POST http://127.0.0.1:8000/v1/question-tasks \
  -H 'Authorization: Bearer demo-analyst' -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: example-question-001' \
  -d '{"question":"逾期率如何计算"}'

REQUEST_ID='替换为上一步返回的 request_id'
curl -s "http://127.0.0.1:8000/v1/question-tasks/${REQUEST_ID}" \
  -H 'Authorization: Bearer demo-analyst'

curl -i -X POST "http://127.0.0.1:8000/v1/question-tasks/${REQUEST_ID}/resume" \
  -H 'Authorization: Bearer demo-analyst' -H 'Content-Type: application/json' \
  -d '{"question":"逾期率如何计算"}'
```

任务 checkpoint 只在 SQLite 保存随机任务 ID、用户 ID 的哈希、状态、意图进度、认证指标版本与引用 ID、Trace ID、尝试次数和过期时间，不保存原问题、令牌、权限凭证、SQL 或查询结果。结果在进程内按用户隔离缓存 15 分钟；每次读取结果都会重新鉴权。checkpoint 闲置 2 小时过期，最长 24 小时；恢复时必须由同一用户重新提交问题，系统重新执行并按当前身份逐个校验工具权限。当前的恢复粒度是整条问答重新执行，不是 LangGraph 节点级续跑。服务重启会将未完成任务标成 `interrupted`；任务元数据可恢复，但原进程内的结果不会恢复。

这是本地单进程实现：最多 4 个工作线程、20 个未完成或仍占用线程的任务；满载提交返回 503 与 `Retry-After: 2`。取消排队任务会尝试撤销执行；取消运行中任务只能阻止后续结果发布，已开始的同步工具仍会运行到结束。后台队列和结果缓存不跨实例共享，任务执行不是持久消息队列语义。生产部署应替换为有持久消费、租约/幂等、加密结果存储与访问审计的任务基础设施；不得把多个 API 实例直接连到同一 SQLite 任务表。迁移契约与状态语义见 `docs/architecture/async-task-reliability.md`。

原同步入口 `/v1/questions` 仍保留给现有脚本与兼容调用；交互页面已改用异步任务接口。

```bash
curl -s http://127.0.0.1:8000/v1/questions \
  -H 'Authorization: Bearer demo-analyst' \
  -H 'Content-Type: application/json' \
  -d '{"question":"2026-03-01 至 2026-03-02 贷款余额按产品查询","session_id":"demo"}'
```

支持口径解释、字段定位、血缘追溯、FINAL/REALTIME 指标查询和连续追问。OpenAPI 页面位于 <http://127.0.0.1:8000/docs>。

### 非认证指标探索试算

普通问答对已认证指标执行认证查询；对候选目录中已登记、未认证且具备安全试算模板的指标，Agent 可根据用户提到的指标和日期自行识别数值探索意图，无需用户输入“探索试算”。例如“2025 年 1-12 月逾期率 Vintage”会按 2025 年各月末观察快照输出 cohort × MOB 矩阵。也可显式输入“探索试算 202606 Vintage逾期率”。MOB 为观察月份与放款月份的日历月差，分子为 `overdue_days > 3` 的逾期本金，分母为原始放款金额。Vintage 专用输入来自隔离的 `exploration_vintage_snapshot` 合成表，不是主数仓中的生产数据。普通申请拒绝率试算只读认证发布的合成离线汇总分区；源记录最多 1,000 行，结果最多 100 行。探索计算不使用通用问答缓存，也不改变认证指标目录。只有候选匹配、日期解析、服务端权限和模板白名单都通过后才执行；模型输出本身不授予权限。定义、血缘、多意图混合或时间含义不明确时，系统仍会解释或要求澄清。

结果会返回 `certification_status=NON_CERTIFIED`、候选版本、更新时间、允许的计算 SQL、候选规则/合成来源引用和审计 Trace ID，页面显著显示“非认证试算”；结果只供业务核对，不得作为认证 FINAL 指标或正式报表。运营和访客身份无探索权限。未知候选、无对应快照或口径/日期歧义仍会澄清、拒绝或返回无数据，不自动猜表字段；业务仍需发起指标建设/治理工单，本系统不自动创建外部工单。

结构化入口要求显式确认非认证状态：

```bash
curl -s http://127.0.0.1:8000/v1/explorations \
  -H 'Authorization: Bearer demo-analyst' \
  -H 'Content-Type: application/json' \
  -d '{"metric_id":"application_rejection_rate","start_date":"2026-03-01","end_date":"2026-03-02","group_by":["biz_date","product"],"accept_non_certified":true}'
```

`Vintage 逾期率`目前只是候选草稿：本地独立合成数据可用于演示规则链路；生产源映射、借据关联唯一性、成功放款过滤、逾期天数来源、历史快照完整性和业务负责人最终签字均未确认。因此它不是生产可用的 NL2SQL，也不是认证口径/血缘。新增指标仍须先登记来源与口径，再增加受审查的沙箱模板和回归用例。

如需真实 Redis 会话状态：

```bash
docker compose up -d redis
CREDIT_MEMORY=redis CREDIT_REDIS_URL=redis://127.0.0.1:6389/0 ./scripts/start_demo.sh
```

默认内存实现仅用于单进程本地演示；多实例环境需要 Redis CAS 或分布式会话锁。

## 阶段 1：真实检索底座

默认启动脚本会使用 Elasticsearch 双路检索（关键词与本地 ONNX 向量）并以 RRF 融合。索引发布是版本化、不可变的：发布前会验证知识目录已生效、模型指纹、35 个分块原文和可见性；只有验证通过才原子切换 `credit-knowledge-current` 别名。服务阶段只加载本地模型缓存，不会因用户请求下载模型。

```bash
.venv/bin/python -m credit.search_index --output var/index-publication.json
.venv/bin/python -m credit.retrieval_evaluation --output var/retrieval-evaluation.json
```

`retrieval-evaluation.json` 是附带的 12 条合成开发/回归用例的结果，分别核验召回、ACL、分块认证原文、生效时间和模型指纹。它不是线上成功率，也没有衡量生成答案的忠实度或完整拒答能力。

## 阶段 2：受控结果缓存与审计

服务会对**无会话的认证知识问答**做精确缓存：键包含用户 ID、角色、数据域、策略版本、原问题和认证目录版本。命中时仍重新校验身份、生成新的 Trace ID 并写入审计；缓存失效或 Redis 故障只会回落到正常处理，不会发布旧结果。页面结果中的 `from_cache` 标识该次是否命中。

阶段 2 的通用缓存刻意不覆盖多轮会话或 `REALTIME` 数据，避免上下文串用及把可能过期的金融数据当作最新结果；FINAL 数值缓存的额外水位线约束见阶段 3。可选值为：

```bash
CREDIT_RESPONSE_CACHE=memory  # 默认本机精确缓存
CREDIT_RESPONSE_CACHE=redis   # 需同时配置 CREDIT_REDIS_URL
CREDIT_RESPONSE_CACHE=off     # 禁用
```

## 阶段 3：FINAL 数据水位线缓存

`FINAL` 数值查询现在可以缓存，但不会只按问题文本命中。服务先读取当前日期范围内、当前数据域可见的 `source_batch` 与 `published_at`，把这份发布水位线连同结构化请求、用户策略和认证目录版本写入缓存键。重发布、补数或发布批次变化都会生成新键，强制重新执行认证 SQL；`REALTIME` 与带会话的多轮查询仍不缓存。

这让缓存节省重复计算，同时不把 FINAL 的旧快照伪装成最新结果。每次命中仍会获得新的 Trace ID 并写审计；页面会显示“缓存命中”。

## 阶段 4：隐私最小化运行观测

`GET /v1/operations/summary` 只允许 `demo-platform` 这类平台身份访问，返回当前进程的聚合完成量、状态/意图计数、缓存命中数和 P50/P95 延迟。它刻意不采集或返回问题、SQL、用户 ID、Trace ID 与结果行；进程重启后重新计数，不把本地演示误当成长期监控系统。

```bash
curl -s http://127.0.0.1:8000/v1/operations/summary \
  -H 'Authorization: Bearer demo-platform'
```

## 阶段 5：本地 SLO 告警演练

`GET /v1/operations/alerts` 同样只允许平台身份读取。它只评估状态，**不会**发送邮件、消息或创建外部事件：

- 样本数小于 `CREDIT_SLO_MIN_SAMPLES` 时返回 `unknown`，避免误报“健康”；
- 已观察到系统失败时返回 `critical`；
- 样本足够且 P95 延迟超过 `CREDIT_SLO_P95_MS` 时返回 `warning`；
- 其余情况返回 `ok`。

默认阈值为 20 个样本和 P95 500ms，仅适合本地演练。阈值、观测结果和告警原因均不包含业务数据或身份信息。

```bash
curl -s http://127.0.0.1:8000/v1/operations/alerts \
  -H 'Authorization: Bearer demo-platform'
```

## 阶段 6：生产身份接入边界

默认 `CREDIT_AUTH_MODE=demo` 只在显式 `CREDIT_DEMO_MODE=1` 时启用本地演示 token。生产模式使用 `CREDIT_AUTH_MODE=oidc`，并需要安装可选依赖和配置可信 OIDC 元数据：

```bash
.venv/bin/python -m pip install -e '.[auth]'
export CREDIT_AUTH_MODE=oidc
export CREDIT_OIDC_ISSUER='https://issuer.example.com'
export CREDIT_OIDC_AUDIENCE='credit-datahouse-api'
export CREDIT_OIDC_JWKS_URL='https://issuer.example.com/.well-known/jwks.json'
```

OIDC 适配器只接受 RS256，使用 JWKS 验证签名，并强制校验 issuer、audience、`exp`、`iat` 与 `sub`。它仅从已验证的 JWT 声明中读取 `roles`、`domains`、`policy_version`；请求体、会话和自然语言都不能授予角色或数据域。此实现是服务端资源校验适配器，不包含登录页面、OAuth 授权码流程或真实 IdP 配置。

## 阶段 7：部署就绪性边界

服务提供三类探针：`/livez` 仅表示进程存活；`/healthz` 返回本地演示状态；`/readyz` 用于是否接收流量的严格判断。`/readyz` 会验证审计数据库和当前配置的检索主链路可访问；若会话记忆配置为 Redis，也会检查该 Redis。响应缓存属于可降级依赖，失联按缓存未命中处理，不会单独阻止接流量。

即使运行时问答可以降级到认证本地词法检索，只要 Elasticsearch 被配置为主检索，ES 不可用时 `/readyz` 仍返回 503。这样部署系统不会把“可有限降级回答”误判为“完整依赖已经就绪”。OIDC 与演示模式也互斥：生产 OIDC 模式不能同时打开演示身份。

默认 `CREDIT_TASK_BACKEND=local` 仍使用 SQLite 元数据、单进程线程池和进程内结果缓存，且只允许单 API worker/实例。可显式切换 `CREDIT_TASK_BACKEND=redis` 做本地多进程验收：API worker 共用 Redis Streams、共享 checkpoint 和独立加密结果键；同用户幂等键、原子登记+入队、队列容量、worker lease/heartbeat、过期接管、取消和显式恢复都在共享状态中处理。`/readyz` 会检查 task Redis 和配置的 Redis 会话记忆。此模式要求 `CREDIT_MEMORY=redis`，否则跨 worker 的 `session_id` 上下文不一致；当前只支持本地 demo 身份解析，生产 OIDC 身份 subject 的共享再解析尚未完成。

阶段 7 的 Redis 后端已接入异步问答 HTTP API。Redis Hash 仅存用户哈希、任务状态、结构化元数据和密文载荷；问题与结果用 AES-256-GCM 加密，幂等索引和请求指纹用密钥化 HMAC，不存原问题、Bearer token 或可复用权限凭证。Lua 原子脚本完成状态+Stream 入队、队列容量检查、单 worker lease claim/续租/接管和结果提交。完成或失败后会 ack 并删除 Stream 中的密文；GET 每次按当前用户重新鉴权。真实 Redis 集成测试覆盖两个 API app 的共享幂等与结果读取、跨身份 404、worker 子进程执行和恢复、取消/迟到结果、Redis 实际密文与租约行为。

任务 Redis 是独立的 Compose 服务（`docker compose --profile task-queue up -d task-redis`），使用 AOF `appendfsync always`、`noeviction`、独立卷和网络，只绑定 `127.0.0.1:6390`。缓存 Redis 的 `allkeys-lru` 设置不会用于任务数据。启动前在根目录 `.env` 设置 `CREDIT_TASK_BACKEND=redis`、`CREDIT_TASK_REDIS_URL`、`CREDIT_TASK_KEYRING` 与 `CREDIT_TASK_ACTIVE_KEY_ID`；生成 32 字节密钥的方式见 `.env.example`。任务模式也要求 `CREDIT_MEMORY=redis` 和 `CREDIT_REDIS_URL`，可指向同一专用本地 Redis。

`CREDIT_DEPLOYMENT_PROFILE=production` 仍强制拒绝启动：当前 Redis Compose 示例没有企业 ACL/TLS，密钥仍由环境注入而非 KMS/密钥托管，异步 worker 的身份重新解析只支持本地 demo user map，未接真实 IdP，故本地验收不构成生产就绪证明。

本地安全启动示例：

```bash
CREDIT_API_WORKERS=1 CREDIT_API_INSTANCES=1 ./scripts/start_demo.sh
```

企业 OIDC 校验器已有适配器，但真实 IdP、密钥托管/KMS、多实例共享队列与恢复演练仍需在目标环境配置和验收；本地模式不能作为生产就绪证明。

```bash
curl -i http://127.0.0.1:8000/livez
curl -i http://127.0.0.1:8000/readyz
```

## 阶段 8：发布前自动验收与交付报告

最后阶段将关键验收收敛为一个 fail-closed 发布门槛：单元测试、静态检查、数仓行为评测与 Elasticsearch 检索/ACL/引用评测必须全部通过。任何一项失败，命令以非零状态退出；成功时只生成脱敏聚合报告，不写入评测原始问题、SQL、用户身份或结果明细。

发布门禁还要求专用任务 Redis 可连接，并单独运行 Streams、共享 task store 与 Redis async API/独立 worker 子进程集成测试；未设置 `CREDIT_TASK_REDIS_URL` 或 Redis 不可用时，报告必须失败（不会把 pytest 的 skip 当通过）。

```bash
docker compose --profile task-queue up -d task-redis
export CREDIT_TASK_REDIS_URL=redis://127.0.0.1:6390/0
export CREDIT_TASK_BACKEND=redis
export CREDIT_TASK_ACTIVE_KEY_ID=local-v1
export CREDIT_TASK_KEYRING='{"local-v1":"<生成并粘贴64位hex密钥>"}'
export CREDIT_MEMORY=redis CREDIT_REDIS_URL=redis://127.0.0.1:6390/0
bash scripts/release_preflight.sh
# 或
.venv/bin/python -m credit.release_preflight --output var/release-preflight.json
```

报告位于 `var/release-preflight.json`，包含目录版本、检查状态、数仓成功率、检索 Recall@5/MRR@5、ACL 泄露数与引用准确率。它是本地合成开发集的可复现验收记录，不是生产 SLA、线上准确率或真实客户数据验证。

### 问答质量评测题集

`credit/data/qa_evaluation.json` 是 50 条带标准答案、证据来源、能力/意图/难度/边界标注的问答开发集，难度比例为简单/中等/困难 40%/40%/20%。覆盖事实、口径、血缘、多跳、对比、计算、模糊和拒答能力，并含无答案、版本冲突、权限、口语、多意图、否定及时间歧义边界。可运行 `.venv/bin/python -m credit.qa_evaluation` 按逐题规则核对回答行为、关键事实、意图、引用和权限；这不是语义判分或线上准确率。2026-10-22 的本地规则结果为 9/50；给定数字演算 S09/S10 已通过，其余主要缺口仍包括通用目录问答、比较、多跳和复杂意图组合。报告默认写入 `var/qa-evaluation-local.json`。详见[题集说明](docs/evaluation/qa-evaluation-set.md)。

## 基础设施增强 1：Spark Local 离线数仓

Spark 接入使用可选依赖，不需要先部署 Hadoop、Hive 或 YARN。它只消费已经通过
`bundle.json + _SUCCESS.json` 原子校验的合成批次，将客户快照、SCD2 维度、DWD、DWS
和 FINAL ADS 写为本地 Parquet，并输出脱敏的六项质量汇总。SQLite 主数仓仍是 API 的
权威演示来源；Spark 产物用于独立验证离线批处理链路，不会绕过既有质量门禁。

```bash
.venv/bin/python -m pip install -e '.[spark]'
.venv/bin/python -m credit.demo --db var/credit.db --sources var/sources
.venv/bin/python -m credit.spark_batch --sources var/sources --output var/spark-warehouse
```

输出目录按层次保存为 `ods/`、`dim/`、`dwd/`、`dws/`、`ads/` 以及
`quality-report.json`。在 16GB 内存设备上，建议仅在运行离线演示时启动 Spark，完成后
退出进程；不要与后续 Kafka、Flink、Doris 全部常驻。当前机器检测到 Java 25，Spark
运行前应优先使用经验证的 Java 17 运行时；若本机验证不通过，再使用 Docker 中的 Java 17。

## 基础设施增强 2：Kafka 单 Broker 实时入口

Kafka 是可选 profile，默认不会常驻占用内存。生产者只发布通过 `LoanEvent` 严格校验的
事件；消费者关闭自动提交，先用既有幂等/版本逻辑更新 `REALTIME ADS`，成功后才提交 offset。
同一 `event_id` 的重复投递、迟到版本和同版本冲突仍由 `replay()` 按既有规则处理。

```bash
.venv/bin/python -m pip install -e '.[kafka]'
docker compose --profile kafka up -d --wait kafka

# 写入一个 LoanEvent JSON 后发布；消费组将其更新到 REALTIME ADS。
.venv/bin/credit-kafka-produce var/event.json
.venv/bin/credit-kafka-consume --db var/credit.db --max-messages 10
```

单 Broker、单分区和副本数 1 只用于本地演示，不提供副本容错、跨可用区可靠性或生产级吞吐。

## 基础设施增强 3–4：Kafka → Flink → Doris 本地实时链路

Flink 和 Doris 使用独立 profile，避免在 16GB 设备上默认常驻。启动前请确保 Spark 任务已经退出；
当前 Docker Desktop 需至少预留 8GB 内存给容器。Kafka 使用主机 `localhost:9092` 和容器内
`kafka:29092` 两个监听地址，主题日志持久化在 `var/kafka-data/`。Doris 的 FE 元数据和 BE 数据
分别持久化在 `var/doris-fe-meta/`、`var/doris-be-storage/`。

本地作业消费单分区 `LoanEvent`，按业务日期和借据号保存最高版本，再按业务日期、产品、数据域
增量维护余额、逾期余额与借据数。重复事件和迟到版本不改动结果；产品或数据域变更会撤回旧组、
加入新组。Doris 使用 UNIQUE KEY 和 Kafka offset 顺序列保存 REALTIME 聚合。该作业使用随项目构建的
单分区 Kafka Source；Flink checkpoint 保存处理状态和下一个 offset，Doris 顺序列使
重复回放收敛到同一结果。每次 Stream Load 使用唯一标签，避免已提交标签在回放时阻塞 HTTP 写入。
它不等同于生产级端到端 exactly-once，当前 checkpoint 也没有配置
跨容器故障的持久存储。

```bash
.venv/bin/python -m pip install -e '.[kafka]'
docker compose --profile kafka --profile flink --profile doris up -d --wait \
  kafka flink-jobmanager flink-taskmanager doris
docker compose exec -T doris mysql -h127.0.0.1 -P9030 -uroot < credit/flink/doris_realtime.sql
bash scripts/build_flink_job.sh
.venv/bin/python -m credit.flink_job
.venv/bin/python -m scripts.verify_flink_pipeline
```

Flink Web UI 位于 <http://127.0.0.1:8081>，Doris FE HTTP 位于 <http://127.0.0.1:8030>。
在 Doris 中查询本地结果：

```bash
docker compose exec -T doris mysql -h127.0.0.1 -P9030 -uroot -e \
  'SELECT * FROM credit_demo.realtime_aggregate ORDER BY biz_date, product, domain'
```

该 Doris 链路是独立验证路径；目前 FastAPI 的 REALTIME 查询仍以 SQLite 演示库为准，
不会把未经跨源对账的 Doris 结果当作 FINAL。Doris 的 `root` 空密码和 Kafka 明文监听仅限
绑定本机端口的合成数据演示；部署到共享环境前需替换身份、网络和连接器。

## RAG 后续：端到端质量评测

`credit-rag-eval` 从认证目录确定性生成并导出 `credit/data/rag_quality_cases.jsonl`，共 78 条合成用例，覆盖 24 条指标口径、8 条血缘、11 条字段、
18 条越权拒答、8 条未认证内容拒答、5 条多指标冲突和 4 条时间澄清。问题不包含真实客户信息。
评测调用实际 `QuestionService` 和 Elasticsearch 混合检索，逐条核对访问状态、精确目录答案、字段定义、
引用来源与版本，以及拒答是否带出数据、SQL 或引用。答案忠实度针对当前目录驱动的确定性回答做精确比对；
项目未配置生成式 LLM，因此报告不代表自由生成答案的事实忠实度。

```bash
.venv/bin/python -m credit.rag_evaluation --output var/rag-quality-evaluation.json
# 若 Elasticsearch 暂不可用，可单独验证路由、权限与目录契约：
.venv/bin/python -m credit.rag_evaluation --local --output var/rag-quality-local.json
```

报告包含数据集哈希、各类通过率、拒答准确率、目录答案一致率、引用一致率和本机延迟分位数。
合成数据上的通过率只用于回归，不等于线上准确率或生产 SLA。

## 规范字典：基础层模型 + 东华源字段

`credit/data/schema_catalog.json` 是两份本地工作簿抽取出的结构快照：基础层模型文档 v1.9 作为统一模型规范，东华数据字典作为源接口及东华数仓结构规范。快照含 17 张基础层表（534 个字段）、37 张东华接口表（787 个字段）和 26 张东华数仓表（1,045 个字段）。语料只含表/字段名、类型、主键/可空标记、更新策略、来源映射和文档出处，不包含业务记录值；接口清单只读取前 12 列，SFTP 路径、账号等列不会被读取。分析员和运营角色可查询表结构，visitor 无权检索。

离线抽取按表生成父块、按字段生成子块，保留工作簿、工作表和字典版本。发布到 Elasticsearch 后，表结构问题返回字段行与原始工作表引用；指标 SQL 查询仍只走原有认证目录及 SQL 模板，不会因为增加表字典而开放任意 SQL。

当源工作簿更新时，安装可选抽取依赖并重新生成快照，再运行结构检索回归：

```bash
.venv/bin/python -m pip install -e '.[knowledge-source]'
.venv/bin/python scripts/build_schema_knowledge.py \
  --base '/Users/lxy/光大信托/字典/光大信托基础层模型文档_v1.9.xlsx' \
  --donghua '/Users/lxy/光大信托/字典/东华数据字典_20210610(1).xlsx'
.venv/bin/python -m credit.retrieval_evaluation --local \
  --cases credit/data/schema_retrieval_cases.jsonl \
  --dataset-label schema-dictionary-regression \
  --output var/schema-retrieval-evaluation.json
```

## 验收

```bash
.venv/bin/pytest -q
.venv/bin/ruff check credit tests
.venv/bin/python -m credit.evaluation --db var/credit.db
.venv/bin/python -m credit.retrieval_evaluation --output var/retrieval-evaluation.json
bash scripts/release_preflight.sh
```

测试覆盖批次幂等、完成标识、防重复加载、SCD2、质量门禁、实时去重与乱序、批流对账、权限隔离、AST 绕过、时点指标语义、会话隔离、记忆降级、审计隔离、熔断恢复、RRF 融合、结果缓存的策略隔离/会话绕过/逐次审计、平台运行摘要的权限与隐私边界、SLO 样本不足/延迟/系统失败状态、存活与严格就绪检查，以及发布前验收的聚合报告与失败闭环。

## 实现边界

- 已运行：SQLite 本地数仓、FastAPI、LangGraph 确定性编排、SQLGlot、审计、内存/Redis 会话适配、实时事件回放和页面。
- 已运行：Elasticsearch 双路检索、ACL 预过滤、RRF、版本化索引、模型指纹和开发集检索评测；知识问答仍由认证目录确定性回答，检索只提供经核对的证据分块。
- 已运行：78 条合成端到端 RAG 质量集，覆盖口径、血缘、字段、ACL/拒答、冲突和时间澄清；本机 ES 评测结果为 78/78 通过。该评测验证目录驱动的确定性回答，不代表生成式 LLM 忠实度或生产效果。
- 已运行：Spark Local Parquet 批处理；Kafka 单 Broker → Flink 单分区版本聚合 → Doris REALTIME 表的本地合成事件链路。
- 未部署：Hadoop、Hive、DolphinScheduler、企业 SSO、高可用和容灾。当前 Kafka/Flink/Doris 是本机演示配置，不是生产集群或容量验证。

## Grill check

- 主要假设：本项目用于作品集和面试演示，重点是可解释的工程闭环，而非复刻生产规模。
- 最大风险：把 Elasticsearch 适配器、事件回放或本地通过率误写成真实生产上线成果。
- 下一决策：若要继续生产化，先选择真实认证数据源和身份提供方，再做检索评测、容量压测、跨实例会话一致性和灾备演练。
