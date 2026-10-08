# OpenResty AI Gateway

可运行的轻量级 MVP：统一 Chat Completions（JSON/SSE/tools）、保守 Fallback、按用户和逻辑模型统计 Provider Token、调用审计和 Prometheus 指标。请求由 Nginx 原生 `proxy_pass` 转发，Lua 实现策略；Redis 管理 Token 账本，独立 Python/SQLite Audit Store 保存脱敏内容。

已实现主链路，OpenSpec 变更仍处于实施/验收状态，未归档。当前只将 LLM 后端明确返回且通过校验的 usage 计入 Token；缺失或非法 usage 标记 unknown，不估算且不记零。用户 Token 硬限额属于后续 M5，当前 `enforcement=false`。

## 启动

需要可视化管理时，使用新增的 [Go + Vue 管理控制台](docs/admin-console.md)：HTTPS 8443、模型独立 URL、用户/Key 管理、草稿发布与回滚，以及用量、日志和内容审计查询。管理能力的 OpenSpec 任务独立于原 MVP，见 [管理端实施任务](openspec/changes/gateway-admin-console/tasks.md)。

需要 Linux Docker Engine 和 Docker Compose v2。Windows 可在 WSL2 终端执行；当前工作区已在 Ubuntu-L4 中完成构建和运行验证。保持 WSL 终端开启，避免发行版空闲退出导致容器停止。

```bash
cp .env.example .env              # 仅首次执行；示例值仅用于本地 Mock
docker compose up -d --build --wait
curl http://localhost:8080/readyz
```

默认仅绑定 `127.0.0.1:8080`，包含两个 Mock Provider，不需要真实模型 Key。Redis、Audit Store、Mock 控制端口和指标端口不映射到宿主机。`.env`、私密运行快照及数据卷不会进入版本控制。

```bash
curl http://localhost:8080/v1/chat/completions \
  -H 'Authorization: Bearer gw-test-user1-key-0000000000000001' \
  -H 'Content-Type: application/json' \
  -d '{"model":"balanced","messages":[{"role":"user","content":"你好"}]}'

curl http://localhost:8080/v1/usage \
  -H 'Authorization: Bearer gw-test-user1-key-0000000000000001'
```

`balanced` 首选 mock-a、备用 mock-b，`local` 直接使用 mock-b。设置 `stream:true` 使用 SSE。返回头含 `X-Request-ID`、`X-Attempt-ID`、`X-Trace-ID`。

可选开启 `fallback.on_key_quota_exhausted: true`，在后端明确额度耗尽时切换不同 Key/模型，默认关闭。详见 [配置与切换日志](docs/key-quota-switch.md)。

## 审计与 Agent 工具

默认 full/100%，记录客户端请求、每次适配后的请求、LLM 回复和工具调用；正文保留 7 天，元数据 30 天。已知凭证和配置中的敏感字段在持久化及摘要计算前脱敏。正文只进入 Audit Store，不进入 Redis 或业务运行日志。

| 接口 | 权限与用途 |
| --- | --- |
| `GET /v1/usage` | `usage:read`；本人按模型拆分及汇总的日/月 Token、剩余、pending/unknown |
| `GET /v1/audit/requests` | `audit:read`；本人请求列表，支持 model/status/from/to/limit/cursor |
| `GET /v1/audit/requests/{request_id}` | 本人事件详情；`include_content=true` 显式读取正文 |
| `GET /v1/audit/traces/{trace_id}` | 本人多轮请求列表 |
| `POST /v1/audit/events` | `audit:write` 且 Key 绑定 Agent；上报工具执行事实 |

查询时间过滤使用 Unix 秒；分页返回 `next_cursor`，必须携带原过滤参数继续查询。事件页最多 100 条并有字节上限。工具/执行摘要最多 100 项，截断有显式标志；完整历史通过事件分页获取。

网关能观察 `tool_calls`，工具实际执行由 Agent 上报 `tool.started/completed/failed`，来源标为 `agent_reported`；未上报显示 `not_reported`。上报不会改变模型 Token。API Key 决定可信 user/agent，客户端不能指定或冒用主体。

```bash
export GATEWAY_API_KEY=gw-test-user1-key-0000000000000001
python examples/agent.py
```

[Agent 示例](examples/agent.py) 展示工具执行、幂等上报、结果回传以及下一轮 trace/parent 关联。权限、恢复及配置细节见 [运行手册](docs/operations.md)。

## 测试与监控

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests/unit -v
bash scripts/test.sh
```

统一测试脚本使用独立项目 `ai-gateway-test` 和端口 18080，运行 Python 单测、真实 Redis 并发测试、OpenResty Lua 测试、网关端到端和故障注入。会停止测试项目的 Redis/Audit Store，并在退出时恢复正常配置；不会删除持久卷。请勿将此测试项目名用于业务部署。

```bash
docker compose --profile monitoring up -d prometheus grafana
```

Grafana 位于 `http://localhost:3000`，首次使用镜像默认管理员登录后更改密码。数据源和 AI Gateway 仪表盘自动配置。Prometheus 仅在容器网络抓取 `gateway:9090/metrics`。当前环境监控镜像下载受限，实际抓取与仪表盘运行验收尚未完成。

## OpenSpec

变更：[openresty-ai-gateway-mvp](openspec/changes/openresty-ai-gateway-mvp/)。按 proposal → specs/design → tasks → apply → verify → archive 工作流推进；只勾选实现和验收证据均齐全的任务。

```powershell
openspec.cmd status --change openresty-ai-gateway-mvp
openspec.cmd validate openresty-ai-gateway-mvp --strict
openspec.cmd instructions apply --change openresty-ai-gateway-mvp --json
```

目录：`lua/` 网关；`redis/` 原子记账；`services/audit_store/` 审计；`config/` 配置；`tests/` 测试；`monitoring/` 监控；`scripts/` 校验/核对/验收工具。
