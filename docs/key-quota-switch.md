# API Key 额度耗尽切换

`fallback.on_key_quota_exhausted` 默认关闭。开启后，同一次 Agent 请求可以在首选 Key 被后端明确拒绝（额度耗尽）时，改用配置中的备用 Key，备用也可以是不同模型。客户端仍使用原 URL、Gateway Key 和逻辑 model，不需要自行重试。

## 开关

在 `config/gateway.example.yaml` 中设置：

```yaml
plugins:
  # 保留其余插件配置
  fallback: true

fallback:
  on_key_quota_exhausted: true

request:
  # 保留其余请求配置
  max_attempts: 2
```

省略新开关等同于 false。关闭后保留原有“明确未发送时才进行连接故障切换”的行为。只修改请求体不能开启这个开关。

## 配置不同 Key 和模型

当前按 `models.<逻辑模型>.candidates` 的顺序选择，最多两个候选（含首次）。每个 Provider 引用一个环境变量 Key；同一服务有两个 Key 时，定义两个 Provider，地址可以相同。

```dotenv
MODEL_KEY_PRIMARY=填写主Key
MODEL_KEY_BACKUP=填写备用Key
```

下面的片段应合并到现有配置，模型名、上下文上限、能力和价格按实际后端填写：

```yaml
providers:
  primary:
    base_url: https://primary.example.com
    key_env: MODEL_KEY_PRIMARY
    quota_exhaustion_codes: [insufficient_quota]
  backup:
    base_url: https://backup.example.com
    key_env: MODEL_KEY_BACKUP
    quota_exhaustion_codes: [insufficient_quota]

models:
  balanced:
    candidates:
      - provider: primary
        model: model-a
        context_tokens: 4096
        max_output_tokens: 1024
        n_max: 1
        input_rate: 1000000
        output_rate: 2000000
        price_version: example-v1
        capabilities: [stream, tools]
        supports_stream_usage: true
      - provider: backup
        model: model-b
        context_tokens: 4096
        max_output_tokens: 1024
        n_max: 1
        input_rate: 1000000
        output_rate: 2000000
        price_version: example-v1
        capabilities: [stream, tools]
        supports_stream_usage: true
```

如果继续保留 `local` 等逻辑模型，应同时保留其 Provider 和预算配置。`base_url` 为根路径，网关追加 `/v1/chat/completions`。不同环境变量解析为同一个 Key 时，不进行额度耗尽切换。

更新 `.env` 和 YAML 后，在 WSL 项目目录执行：

```bash
docker compose up -d --build --force-recreate --wait
```

## 识别规则和边界

- 仅识别 HTTP 402/429、完整 JSON、正文不超过 64 KiB 的错误；`error.code` 或 `error.type` 必须精确匹配该 Provider 的 `quota_exhaustion_codes`。
- 默认错误码列表只有 `insufficient_quota`。其他兼容服务可配置自己的明确额度耗尽码，如 `credits_empty`；不要将普通 `rate_limit_exceeded` 加入列表。
- 不根据 HTTP 状态或错误 message 的文字猜测额度耗尽。普通限流、401、5xx、畸形/不完整/过大的错误不因该开关重试。
- 成功 JSON/SSE 仍逐块转发；已经发送给客户端的响应不能切换或续写。流式中途的额度错误也不会跨模型拼接。
- 备用须满足 stream/tools/输出上限等能力要求，且通过自己的预算和审计准入；额度耗尽不绕过网关预算。
- 备用也耗尽时返回备用错误，不循环；没有不同 Key 的兼容备用时返回原错误。
- 原失败尝试的有效 usage 仍会计费；缺失 usage 标记 unknown，保留预占供核对，不自动记零。
- 当前没有持久 Key 冷却池，每个新请求仍从首选候选开始；此开关负责单次请求内的自动切换。

开启时增加一个仅绑定容器 loopback 的 Nginx relay，由它以原生 `proxy_pass` 调用后端。Lua 在客户端收到响应前检查错误；成功内容按块透传，不缓冲完整成功流。8081 不对宿主机或容器网络开放。

## 日志与审计

```bash
docker compose logs gateway | grep 'fallback.selected'
```

额度切换日志包含 `reason=quota_exhausted`、匹配错误码、request_id/trace_id、前后 attempt_id、Provider、实际模型及 `from_key_ref`/`to_key_ref`（环境变量名）。不记录密钥值或原始错误 message。`fallback.selected` 表示选择了备用尝试，不保证备用最终成功；结合该 attempt 的 `attempt.finished` 查看结果。

请求详情审计保留两个尝试和原额度错误的脱敏正文；客户端只收到最终结果。`gateway_fallback_total` 也会累计此次切换。
