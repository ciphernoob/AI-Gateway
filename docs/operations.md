# 运行与恢复

## 配置与真实后端

编辑 `config/gateway.example.yaml`，或通过 Compose override 将自己的 YAML 挂载至 config 服务的 `/config/gateway.yaml`。配置校验器拒绝重复 YAML Key、未知用户或模型引用、金额及价格字段、无效时区、缺失凭证和插件依赖缺失。

Provider `base_url` 是服务根路径，网关追加 `/v1/chat/completions`。目前支持 OpenAI-compatible Chat Completions，不转换其他厂商原生协议。真实 Provider 需要配置准确的 context/output/capabilities、凭证，并确认 JSON 或 SSE 响应会返回 usage。

网关只统计后端明确返回且通过校验的 `prompt_tokens`、`completion_tokens`、`total_tokens`。三者必须是非负安全整数，并满足输入加输出等于总量。网关不使用本地 tokenizer，不按正文长度估算，不配置模型价格，也不维护金额预算或费用账本。

Token 账本按可信 `user_id`、逻辑模型和尝试开始时的 Asia/Shanghai 日/月窗口分组。日桶默认保留 35 天，月桶默认保留 400 天。`/v1/usage` 返回各模型明细和用户汇总；存在 pending 或 unknown 时，`remaining_is_exact=false`。Token 配额只用于统计展示，不执行硬拦截。

## 配置更新和回退

先备份 YAML、`.env` 和持久卷。校验成功前不会替换旧运行快照：

```bash
docker compose run --rm config
docker compose exec -T gateway openresty -p /app/ -c nginx/nginx.conf -t
docker compose exec -T gateway openresty -p /app/ -c nginx/nginx.conf -s reload
docker compose restart audit-store
```

管理模式应通过 HTTPS 控制台发布。Go 管理端调用 Python 校验器生成不可变快照，再由内部控制代理检查并平滑重载 OpenResty。旧 worker 完成已经开始的请求，新 worker 使用新版本。普通 `docker compose down` 保留数据卷；升级和回退不要使用 `down -v`。

从包含金额字段的旧管理数据库升级时，管理端会在读取和编译草稿或历史版本时确定性删除 `budget`、`input_rate`、`output_rate` 和 `price_version`。新 YAML 或管理 API 请求若继续提交这些字段，会被明确拒绝。

## 故障语义

- 默认只在连接失败且 Nginx 明确记录发送和接收字节均为零、没有上游响应头时切换候选。可选 `fallback.on_key_quota_exhausted` 允许明确额度耗尽的 402/429 在输出前切换不同 Key。
- Redis 不可用或待结算容量达到上限时，新模型调用失败关闭，不访问后端。Redis 使用 AOF 和 noeviction。
- 审计开始证据持久化失败时不发送 LLM 请求。最终审计和 Token 结算由 timer 队列提交，队列有条数、字节和重试上限。
- 明确未发送到后端的尝试只关闭 pending，不写 Token，也不增加 unknown。可能已经发送但没有取得 Provider usage 的尝试增加 unknown。
- JSON 或 SSE 返回非法 usage 时不写数值；流在最终 usage 到达前中断也记 unknown。不能通过审计正文或人工填写补算 Token。
- worker 异常退出可能丢失尚未进入结算队列的 usage。恢复器只能根据 Redis dispatch 状态关闭未发送尝试或标记 unknown，不能重建后端未持久化的返回参数。

`/livez` 只表示进程可响应；`/readyz` 同时检查 Redis 和启用的 Audit Store。`gateway_unsettled_attempts`、`gateway_settlement_failures_total`、`gateway_unknown_usage_total` 和 `gateway_audit_queue_bytes` 反映计量与审计健康状况。业务日志记录关联 ID、可信身份、模型、状态和 Provider Token，不记录请求正文、密钥或费用。

## 审计读取与限制

同一 trace 的父请求必须属于本人且 trace 一致。未知和他人资源统一返回 404。request/attempt ID 由网关生成，客户端正文中的 user_id 或 agent_id 不决定统计主体。Agent 上报必须指向网关已经观察到的 tool_call。

Audit Store 分离 status、capture_state 和 payload_state。正文到期后保留摘要和过期状态；审计正文不是 Token 账本来源。单请求配置最大 4 MiB，单响应审计最大 4 MiB、工具事件最大 256 KiB。指标使用有限标签，不包含 user、agent、request、attempt 或 trace。

Audit Store 是单机 SQLite WAL，适用于当前单实例 MVP。高吞吐、多副本、严格持久化投递和可证明防篡改需要另行设计持久消息队列和存证存储。
