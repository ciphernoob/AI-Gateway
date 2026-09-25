# 运行与恢复

## 配置与真实后端

编辑 `config/gateway.example.yaml`，或通过 Compose override 将自己的 YAML 挂载至 config 服务的 `/config/gateway.yaml`。配置校验器拒绝重复 YAML Key、未知用户/模型引用、负数价格、无效时区、缺失凭证以及插件依赖缺失。凭证从 `.env` 对应环境变量解析；Gateway Key 用 SHA-256 摘要认证，但已知凭证脱敏清单和 Provider 密钥仍存在私密运行卷中，因此该卷仅对网关/内部服务开放。

Provider `base_url` 是服务根路径，网关追加 `/v1/chat/completions`；例如 `https://api.openai.com`，不要重复写 `/v1`。目前支持 OpenAI-compatible Chat Completions，不转换 Anthropic 原生协议或 Responses API。HTTPS 开启证书和 SNI 校验。真实 Provider 需要配置准确的 context/output/capabilities、单价和凭证，并单独验收其兼容性。

`input_rate`、`output_rate` 是每百万 Token 的 micro-USD 价格。一次实际费用为 `ceil((input_tokens*input_rate+output_tokens*output_rate)/1000000)`；预算和结算均为整数 micro-USD（1 USD = 1,000,000 micro-USD）。预占按配置上下文上界、输出上限及 n 保守计算，可能比真实输入高。每次尝试保存自己的价格快照。

Budget 分 global、可信 Agent、logical model 三个维度；无 Agent 绑定的 Key 只使用 global/model。`budget.epoch` 是账本命名空间，正常调价/改限额不要改它。当前金额预算累计至人工切换 epoch，不自动周期重置。

User Quota 与金额预算独立。窗口按尝试开始时刻、固定 Asia/Shanghai 归属；日桶保留 35 天、月桶 400 天。`remaining_tokens` 由已知用量计算，存在 pending/unknown 时 `remaining_is_exact=false`。断流或 Provider 未返回 usage 不记成零；失败的后端尝试也保留。

## 配置更新和回退

先备份 YAML 与 `.env`，保留原镜像 ID。校验/生成快照成功前不会替换旧文件：

```bash
docker compose run --rm config
docker compose exec -T gateway openresty -p /app/ -c nginx/nginx.conf -t
docker compose exec -T gateway openresty -p /app/ -c nginx/nginx.conf -s reload
docker compose restart audit-store
```

Audit Store 启动读取策略；已有 trace 使用创建时的策略快照，新 trace 使用新策略。切换服务密钥应安排维护窗口，同步重启网关/Store。网关策略热重载不重置 Redis spent；旧 worker 的进行中请求使用旧配置。回退时恢复 YAML/.env/镜像，重新生成配置并重启，保留 `redis-data`、`audit-data` 卷。普通 `docker compose down` 保留卷；保留数据的升级/回退流程不要使用 `down -v`。

## 故障语义

- 默认只在连接失败且 Nginx 明确记录发送/接收字节均为零、没有上游响应头时切换。可选 `fallback.on_key_quota_exhausted` 允许明确额度耗尽的 402/429 在输出前切换不同 Key，详见 [额度切换](key-quota-switch.md)。普通限流/5xx、已发送后超时和已输出 SSE 不自动重放。主备最多两次。
- Redis 不可用、待结算容量达到上限时，新模型调用失败关闭。Redis 使用 AOF 和 noeviction，不驱逐未结算数据。
- 审计开始证据持久化失败时不发送 LLM 请求。最终响应和结算在 timer 中提交，队列有条数/字节上限和有限重试。
- worker 退出可能丢失尚未提交的响应。Store 超过 deadline+10 秒未收到最终记录时标 `incomplete`；它不是无损取证系统或外部防篡改存证。
- Redis 扫描开始标记：明确未 dispatch 的尝试按 system/0 释放；dispatching 但缺最终用量的尝试保留 unknown 和金额预占。不凭租约到期把已发送请求记零。
- 当前 Nginx 原生读/写超时是空闲超时；总期限用于准入、Fallback、连接超时上界和收到响应块时截止检查。**尚未实现静默连接上的独立绝对截止计时器**，最坏可能多等一个 read timeout。严格绝对截止验收保持未完成。

`/livez` 只表示进程可响应，`/readyz` 同时检查 Redis 和启用的 Audit Store。`gateway_unsettled_attempts`、`gateway_settlement_failures_total`、`gateway_audit_queue_bytes` 反映积压。业务日志只记录结构化身份/关联 ID、模型、状态、Token 和金额；Nginx 自身诊断仍是原生格式。

## 离线核对

只能由能访问运行配置和内部网络的运维者执行，没有公共管理 API。准备仅含 prompt_tokens/completion_tokens/total_tokens 的精确 JSON（证实未发送可为全零），凭 Provider 证据核对，不根据猜测释放 unknown 预占：

```bash
docker compose run --rm test python -m scripts.reconcile \
  --attempt-id attempt_REPLACE \
  --usage-file /app/private-usage.json \
  --evidence invoice-or-ticket-123
```

工具使用 attempt 的价格和窗口快照，先持久化 prepared 证据，再分别幂等更新 quota/budget，最后追加 applied 证据。中途失败后修复依赖并重复同样输入和证据引用；冲突值拒绝覆盖。核对记录独立保留 400 天，可处理超过请求审计 30 天的未结算账本，不复活过期正文。不要将凭证、prompt 或个人内容放入 evidence 或提交核对文件。

## 审计读取与限制

同一 trace 的父请求须属于本人且 trace 一致。未知和他人资源统一 404。request/attempt ID 由网关生成，客户端 user_id/agent_id 不决定计费主体。Agent 上报须指向已观察到的 tool_call；尚未落地时返回 `audit_pending`，应只重试投递，不重跑工具。

Store 分离 status（调用完成/失败/中断）、capture_state（pending/complete/incomplete）和 payload_state（captured/redacted/truncated/not_captured/expired）。正文到期后保留摘要和过期状态，读取校验摘要；读取行为单独留痕，不再递归采集正文。配置字段和已知 Key 脱敏不等于自动识别任意自然语言隐私。

单请求配置最大 4 MiB，单响应审计最大 4 MiB、工具事件 256 KiB。超出审计限制不阻断已开始的 SSE，用量采集继续。指标使用有限标签，不含 user/agent/request/attempt/trace。

Audit Store 是单机 SQLite WAL、串行写事务，适用于 MVP。高吞吐、多副本、严格持久化投递和可证明防篡改需另行设计持久消息队列及存证存储。Mock 的控制接口仅限隔离开发网络。
