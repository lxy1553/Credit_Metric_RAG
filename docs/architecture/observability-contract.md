# 本地问答 Trace 契约（阶段 6）

此契约针对本地合成数据验证项目；Trace 不是生产级分布式追踪，审计表才是必需的合规记录。

## 关联与可见性

- 同步请求返回 `trace_id`；异步任务通过 `request_id` 查到结果后取得对应 `trace_id`。`GET /v1/traces/{trace_id}` 先按审计表核验当前用户归属，其他用户看到 404。
- 根 span 记录请求总耗时；意图分支 span 记录分支状态、耗时、固定错误码、认证证据 ID 和本次实际检索 backend。并行分支 backend 不同时，顶层显示 `mixed`，详情以各分支 span 为准。
- `router_mode` 表示本次实际路径：模型分类、确定性回退、会话规则、缓存命中、结构化入口或无需路由。不能把配置了 DeepSeek 等同于本次用了 DeepSeek。

## 模型来源字段

| 字段 | 含义 | 缺失时 |
|---|---|---|
| `model` | 请求配置的模型 ID；模型失败回退时仅说明尝试了该模型 | `null` |
| `response_model` | 本次已接受的 DeepSeek 响应中的 `model` | `null` |
| `system_fingerprint` | 本次已接受的 DeepSeek 响应中的后端配置指纹 | `null` |

DeepSeek [Chat Completions API 文档](https://api-docs.deepseek.com/api/create-chat-completion/)把 `system_fingerprint` 定义为后端配置指纹。它可辅助定位环境变化，但**不是**供应商承诺的不可变模型权重或发布版本；任何字段缺失都保持 `null`，不由请求模型 ID、时间戳或上次调用推测。当前测试使用本地模拟响应，未把它冒充真实供应商联通验收。

## 隐私、留存与故障语义

Trace 仅持久化白名单字段。证据 ID、路由/检索模式和模型元数据按字符与长度过滤，不保存原始问题、SQL、回答、查询结果、凭证或用户 ID。新 Trace 写入时清理 30 天前的 span；审计表不受该留存策略影响。Trace 写入失败不改变已审计答案，审计写入失败则拒绝发布答案。
