## Why

Agent 接入多个模型后，需要一个稳定入口统一鉴权、转发、故障处理和费用管理。当前仓库为空，先建设基于 OpenResty 的轻量级 MVP，并提前分离金额预算与用户 Token 配额，避免后续扩展时重复采集用量或混用额度。

## What Changes

- M0：建立 OpenResty/Lua 插件骨架、Docker Compose、Mock Provider、静态 YAML 配置和 API Key → user_id 身份识别。
- M1：实现 `POST /v1/chat/completions`，支持逻辑模型映射、JSON、SSE 和工具调用字段，使用 Nginx 原生代理接入两个 OpenAI-compatible 后端。
- M2：实现有序主备、有限尝试和独立 attempt_id。默认仅在能够证明请求未发送时重试，响应已发给客户端后不得切换。
- M3：公共 Usage Collector 统一生成逐次尝试的用量事件；User Quota 原子统计每用户日/月 Token；Budget Engine 独立完成全局、可信 Agent、模型的金额检查、预占与幂等结算。
- M4：提供只读本人 `GET /v1/usage`、结构化运行日志、Prometheus 指标、Grafana 仪表盘，以及包括故障和并发场景的集成测试。
- M4 新增独立 Audit & Trace（调用审计与追踪）模块：记录请求 messages、模型参数、LLM 回复及工具调用指令；按尝试重组 SSE，关联多轮模型调用，并通过认证接口接收 Agent 工具执行开始、结果与异常。提供本人审计查询、正文脱敏/保留策略及记录完整性状态，作为网关可审计性的交付内容。
- 缺失 usage 明确标记未知，不计成精确零；未知费用保留金额预占，并提供恢复及人工核对入口。
- 用户追加：默认关闭的额度耗尽切换开关；明确额度拒绝后可按候选顺序切换不同 Key/不同实际模型，保留结构化日志和每次尝试审计。智能语义降级仍不在范围内。

### Non-goals

M5 的用户 Token 硬限额、Token 原子预占及超限拦截不在本变更内。MVP 已有日/月统计窗口，但不实现 M5 的硬配额周期重置状态机。智能路由、跨模型语义降级、多厂商原生协议转换、管理后台、管理员跨用户 HTTP 查询、多币种兑换、Redis Cluster 及生产级持久化事件总线均为后续变更。审计模块不主动执行工具、不采集绕过网关且未上报的工具活动，也不承诺 WORM 存储、法律级不可抵赖或 worker 崩溃前未提交内容的无损恢复。

## Capabilities

### New Capabilities

- `gateway-runtime`：运行骨架、插件生命周期、配置校验、Mock 与健康检查。
- `user-identity`：静态 Gateway API Key 认证、可信用户与 Agent 映射、凭证隔离。
- `unified-api`：统一 Chat Completions、模型注册表、原生 HTTP/SSE 转发与一致错误。
- `provider-fallback`：保守重试、尝试级上下文、主备切换与流式边界。
- `usage-collection`：JSON/SSE 用量提取、标准事件、未知状态和异步消费。
- `user-quota`：用户日/月 Token 幂等统计、配额配置与本人查询。
- `monetary-budget`：独立金额账本、原子多维预占、结算与异常恢复。
- `gateway-observability`：运行日志、指标、仪表盘，以及独立 Audit & Trace 模块的内容审计、工具事件、查询与完整性契约；两类模块在实现与存储上分离，沿用该能力规格管理关联要求。

### Modified Capabilities

无。仓库目前没有已有实现或已归档规格。

## Impact

新增代码预计位于 `nginx/`、`lua/core/`、`lua/plugins/`、`lua/adapters/`、`redis/`、`config/`、`scripts/`、`tests/`、`monitoring/` 和 `services/audit_store/`，以 `docker-compose.yml` 启动 Linux 容器。运行依赖为 OpenResty、Redis、Prometheus、Grafana 及基于 Python/SQLite 的内部 Audit Store；构建阶段将 YAML 转为经校验的 Lua 可读配置，敏感值通过环境变量引用。审计正文与元数据进入独立持久卷，Redis 继续只管理计量和预算等运行状态。

公开接口包括 `POST /v1/chat/completions`、`GET /v1/usage`、`POST /v1/audit/events`、`GET /v1/audit/requests`、`GET /v1/audit/requests/{request_id}`、`GET /v1/audit/traces/{trace_id}` 和最小健康检查；审计业务接口均认证且仅访问本人数据，`/metrics` 与 Audit Store 仅暴露到内部网络。配置、价格版本与运行账本分别管理，Agent 上报不能改写网关事实或费用。测试使用两个独立 Mock 后端和 Mock Agent，无需真实 Provider 密钥或付费调用。
