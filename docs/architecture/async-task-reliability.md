# 异步问答任务可靠性契约

本文区分默认本地演示后端、可选的本地 Redis 多进程验收后端和生产边界。两种本地后端共享 HTTP 状态语义，但可靠性能力不同。

## 状态与 API 契约

| 状态 | 含义 | 后续动作 |
|---|---|---|
| `queued` | 任务已登记，等待 worker | 可轮询或取消 |
| `running` | worker 已取得租约并执行 | 可轮询；取消阻止结果发布，但不能强行中断已开始的同步调用 |
| `completed` | 结果已安全提交 | 仅同一用户可读取；每次读取重新鉴权 |
| `failed` | 执行失败，只保存安全错误信息 | checkpoint 有效时同一用户可重新提交问题恢复 |
| `interrupted` | worker/进程中断，或结果不可再安全读取 | 同一用户重新提交问题，按当前身份重新执行 |
| `cancelled` | 结果不得发布 | 原任务不可恢复，需新建任务 |

提交 API 返回 `request_id`；状态、结果、恢复和取消由异步任务接口管理。恢复会重新执行整条问答，不是 LangGraph 节点级续跑。checkpoint 只保存最少任务状态和结构化进度，不保存可复用权限凭证。跨用户访问统一表现为 404；过期任务返回 410。

## 后端差异

### 默认 `local`

- SQLite 保存 checkpoint，单进程线程池执行，结果保存在进程内存。
- 默认最多 4 个工作线程、20 个活跃任务；满载返回 503 与 `Retry-After`。
- SQLite 幂等记录按用户隔离，绑定问题与 `session_id` 的加盐摘要；相同请求重试返回原任务，不同请求冲突。
- checkpoint 闲置 2 小时过期，最长 24 小时；结果缓存 15 分钟。
- 服务重启后队列和结果不保留，未完成任务转为 `interrupted`，只能重新提交问题恢复。
- 只支持一个 API worker/实例，不可让多个进程共用 SQLite 任务后端。

### 可选本地 `redis`

设置 `CREDIT_TASK_BACKEND=redis` 后，异步问答 API 使用 `RedisQuestionTaskManager`，并可由多个 API 进程共享任务状态和结果。当前只作为本地验收模式：要求本地 demo 身份映射、任务密钥环境注入、`CREDIT_MEMORY=redis` 及共享 Redis 会话记忆。

- 独立 `task-redis` Compose 服务只绑定 `127.0.0.1:6390`，使用独立卷/network、AOF `appendfsync always`、`noeviction` 和 256 MiB 上限；它不是 HA 或生产集群。
- 登记任务与 Redis Stream 入队在同一 Lua 原子提交中完成；同用户幂等冲突、容量上限和 request fingerprint 同步处理。
- 问题载荷和结果以 AES-256-GCM 加密，随机 nonce，认证数据绑定 request_id/key id；幂等指纹与 owner 标识使用 HMAC。Redis 元数据不含原问题、Bearer token 或可复用权限凭证。
- 工作进程通过 Redis Stream 消费，并用状态存储中的 worker lease 做原子认领、续租和 fencing；worker 崩溃后允许 lease 超时接管。终态完成或失败会 ack 并删除 Stream 密文消息。
- 结果保存在独立加密 Redis key，带 TTL。每次 checkpoint/结果读取按当前用户校验 owner；恢复必须由同一用户重新提供问题，重新走身份与工具权限检查。
- 取消、失败、超时与过期迁移会清理不应继续暴露的密文/结果；迟到 worker 不能越过取消状态或失效租约发布结果。
- task Redis 或 Redis 会话记忆故障会使 `/readyz` 不就绪；不会静默回退到另一个实例不可见的内存状态。

任务密钥通过 `CREDIT_TASK_KEYRING` 与 `CREDIT_TASK_ACTIVE_KEY_ID` 环境变量注入，示例仅放在 `.env.example`，真实密钥不能提交到仓库或日志。密钥轮换期间需保留仍有效数据所需的旧 key；当前 HMAC 幂等指纹也依赖密钥环，因此轮换策略必须避免有效 checkpoint 存续期内更换指纹密钥。

## 可靠性边界与生产限制

本地 Redis 验收验证了多 API app 共享同一任务、跨身份隔离、独立 worker 进程执行/恢复、lease fencing、原子幂等入队、密文存储和 Redis 故障 readiness。它不提供副本高可用、备份恢复演练、企业 Redis ACL/TLS、KMS 托管、真实 OIDC subject 重新解析、死信/运维告警或生产容量承诺。

因此 `CREDIT_DEPLOYMENT_PROFILE=production` 必须继续 fail-closed。不要把本地 Compose 的 AOF 或环境变量密钥描述为生产级安全/持久性。生产启用前必须补齐企业 Redis/KMS/身份集成、备份恢复与灾备验证、告警与容量基准，并重新执行安全和权限评审。

## 本地验收

启动独立任务 Redis：

```bash
docker compose --profile task-queue up -d --wait task-redis
```

在本机 `.env` 配置 Redis backend、`CREDIT_TASK_REDIS_URL`、AES keyring/active key id，以及共享 Redis session memory；变量名和生成本地随机 key 的方式见 `.env.example`。不要把实际 key 写入文档、命令历史或提交记录。

运行：

```bash
CREDIT_TASK_REDIS_URL=redis://127.0.0.1:6390/0 bash scripts/release_preflight.sh
```

发布预检会检查 unit suite、Ruff、任务 Redis 连通性、Redis Streams/store/API 的真实集成测试、数仓开发集和检索评测。Redis 未配置或不可达时门禁失败；即使 Redis 在预检探测后掉线，集成测试也会 fail 而不是 skip。报告只输出脱敏汇总，位于 `var/release-preflight.json`。
