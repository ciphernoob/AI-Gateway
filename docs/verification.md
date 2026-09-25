# 实施验收与 OpenSpec 核对

日期：2026-09-24。变更：`openresty-ai-gateway-mvp`，schema：spec-driven。

**结论：可运行初版已交付；完整 MVP 验收尚未结束，不应归档。** OpenSpec 的 planningComplete 表示规划产物齐全，不代表下列实施任务全部完成。

## 核对结果

| 维度 | 结果 |
| --- | --- |
| 完整性 | 42/64 实施任务完成；22 项包含未完成实现或未覆盖的验收条件 |
| 正确性 | 8 项能力均有代码与测试入口；37 个 Requirement、76 个 Scenario 已对照，未宣称逐场景全覆盖 |
| 一致性 | 保持 OpenResty 原生代理、独立金额/Token 账本、独立 Audit Store、可信身份、尝试级计量与审计边界 |
| 自动化 | Python 27 项、真实 OpenResty Lua 检查、8 项原故障探针、6 组额度切换配置测试 |
| 归档 | 不执行；先完成剩余任务与实际监控验收 |

## 已运行证据

追加功能 9.1–9.5 已完成：默认关闭的额度耗尽切换、402/429 结构化错误识别、不同 Key/模型备用、成功 SSE 逐块转发和安全日志。新验收输出为 [key-quota-acceptance.log](../test-results/key-quota-acceptance.log)，日志为 [key-quota-gateway.log](../test-results/key-quota-gateway.log)。27 项 Python 测试、Lua 检查、8 项原故障探针及 key_quota_off/on/same/single/budget/custom 全部通过；日志检查覆盖 92 条请求/尝试事件和 7 条切换事件，未发现已知测试密钥值。跨模型切换、两次耗尽、未知用量保留、有效失败 usage 入账、普通限流/401/5xx/大错误/流中断不切换、相同 Key 不切换、次数上限、备用预算不足及自定义错误码均已验证。开发配置继续保持开关 false。原有 22 项待办未被此次范围替代。

在 Windows PowerShell + WSL2 Ubuntu-L4（Ubuntu 24.04）中使用 Docker Engine/Compose v2；OpenResty 1.29.2.4、Redis 7.4.2、Python 3.11.11 的基础镜像按 digest 固定。

```bash
bash scripts/test.sh
python -m scripts.check_test_logs test-results/gateway.log
```

完整本地输出：[acceptance.log](../test-results/acceptance.log)（忽略提交的运行产物）。网关日志：[gateway.log](../test-results/gateway.log)。日志校验确认有标准化业务事件，未发现已知测试凭证或消息/工具正文结构。不要把该测试当作任意自然语言敏感信息识别。

| 能力规格 | 实现证据 | 已验证内容 |
| --- | --- | --- |
| gateway-runtime | Dockerfile、Compose、scripts/validate_config.py、lua/gateway.lua | 构建、nginx -t、健康检查、配置错误拒绝、Redis/审计不可用 |
| user-identity | lua/plugins/identity.lua | Key/scopes、禁用、同用户多 Key、身份伪造不改变归属、跨用户隔离 |
| unified-api | lua/plugins/unified_api.lua、nginx/proxy.conf | 两个不同凭证后端、模型映射、JSON/tools、逐块 SSE、大小/n/输出边界 |
| provider-fallback | lua/plugins/fallback.lua、lua/gateway.lua | 真实命名 location、拒绝连接零发送切换、两次失败、单次上限、429/5xx/发送后超时/断流不重放 |
| usage-collection | lua/core/response_observer.lua、usage_event.lua、settlement.lua | Unicode/CRLF/多行/跨块、重复 usage、unknown 与 system/0、版本事件、计量与审计上限分离 |
| user-quota | redis/accounting.lua、lua/plugins/user_quota.lua | 100 并发与重复、unknown 补录、冲突拒绝、TTL、过期桶、本人查询、超限不拦截 |
| monetary-budget | lua/plugins/budget.lua、redis/accounting.lua、scripts/reconcile.py | 100 抢 10、多维无部分更新、100/60 释放、超估算、独立消费者失败重放、核对审计与幂等 |
| gateway-observability | services/audit_store/、lua/plugins/audit.lua、observability.lua、examples/agent.py | 内容脱敏、身份/trace、工具上报/冲突/乱序/期限、查询隔离/游标、7/30 天过期、摘要、metrics 隔离、关闭指标仍审计 |

故障探针：fallback、both_down、single_attempt、budget_denied、quota_exceeded、metrics_off、audit_down、redis_down。Agent 示例已实际运行两轮并生成 trace；不是仅检查语法。

## CRITICAL：归档前需完成

1. **严格总期限未实现独立取消计时器**。见 `lua/gateway.lua:163`、`lua/gateway.lua:177`：当前为 Nginx 连接/读写超时、Fallback 期限及每块响应检查，静默连接可能超过总期限一个 read timeout。任务 2.5/3.3 保持未完成；应增加绝对截止实现并以静默/缓慢连续流验证。
2. **Prometheus/Grafana 运行验收缺失**。配置和仪表盘已生成，公共端口隔离及指标文本已测；本机 Docker Hub/镜像缓存下载超时，未成功运行监控容器。任务 8.4 保持未完成；需在可拉取镜像的环境运行 promtool、检查 scrape target 和各面板。
3. **故障恢复极端场景未全部覆盖**。Redis claim/dispatch 竞态和 Store 受控时钟 incomplete 已测，但尚未完成真实 worker 强杀、队列饱和、timer 创建失败、磁盘满、长时间 Redis 暂断恢复的组合验收。任务 4.6、6.6、7.7 等保持未完成；不得将异步最终内容描述为不丢失。
4. **其余未勾选任务的所有验收条件仍须补齐**。下列各项分别视为归档阻断项，已有部分代码不等于任务全项完成：

- **1.3**：实现 RequestContext、插件依赖和阶段调度；验证可选插件单独开关、依赖缺失拒绝及强制鉴权规则。
- **1.5**：实现两个独立 Mock Provider 的 JSON、工具调用、分块 SSE、429/500、超时/断流与 usage 异常；以测试直接访问 Mock 验证行为及调用计数。
- **2.5**：实现统一网关错误、X-Request-ID、连接/读取/请求总时限；验证 400/401/413/502/504，以及 Provider 错误不会泄漏内部配置。
- **3.3**：实现“明确零发送且无上下游响应”的切换判定与请求总时限；验证连接拒绝/连接超时可切换，缺失或模糊字节证据禁止切换。
- **3.6**：完成 M2 故障矩阵并保存尝试日志证据；验证两候选均失败、能力不匹配、不同凭证切换、客户端断开与两个尝试的关联。
- **4.4**：实现调用状态与首实际内容时间采集；验证 role/空 delta 不计 TTFT，断流及客户端取消保留 unknown，真实 Provider 精确零仍可识别。
- **4.6**：实现 log 阶段事件复制、timer 回调、有限退避和消费状态；验证禁止网络阶段没有 cosocket 调用，timer 失败/队列满/Redis 暂断均可见。
- **5.1**：实现 users 限额配置、日/月窗口与固定 Asia/Shanghai 时区；验证跨午夜、月末和延迟结算均归入尝试开始窗口。
- **5.5**：实现查询所需一致快照和剩余额度计算；验证空记录、无限额、已超限、unknown/pending 与 Redis 故障的返回语义。
- **6.1**：实现整数金额、价格快照、上下文上界预占、输出上限和 n 计费规则；验证舍入、溢出、价格重载和无 Agent 绑定时的归属。
- **6.3**：在每次实际转发前接入预算准入；验证 Redis 不可用失败关闭、余额不足返回 budget_exceeded、配置重载不重置 spent。
- **6.5**：将主备尝试接入各自预算事务；验证主尝试证实未发送时释放、备用按自身价格预占、备用预算不足不触达上游。
- **6.6**：实现 lease 扫描、恢复领取与未知预占保留；验证 worker 退出、最终事件丢失、多 worker 恢复，以及关闭金额插件时仍不误判用户用量为零。
- **6.8**：配置 Redis AOF/noeviction 与未结算容量保护，完成跨账本故障集成测试；验证 Redis 暂断恢复、脚本缓存失效重载、错误 key 类型和未解决记录保留。
- **7.5**：实现独立 audit 插件及共享响应观察器的内容收集，重组 JSON/SSE、多 choice 与交错工具参数；验证首块及时转发、超限/断流显式标记、后续 usage 仍正常结算。
- **7.6**：实现按 trace 固定采样、字段及已知凭证脱敏、metadata_only/off、内容限额；验证 secret 跨片段不落盘、不能安全脱敏时有明确缺口，客户端不能降低策略且转发正文不被改写。
- **7.7**：实现有界字节/条数队列、异步最终提交、幂等重试和 Store 超期 incomplete 扫描；注入 timer/队列/磁盘故障、worker 退出及晚到最终事件，验证不伪造 complete、不重试 LLM、不改变账本。
- **7.12**：完成审计故障及权限验收矩阵，交付 API 示例、内容策略/恢复/保留说明和吞吐开销测量；验证关闭 metrics 不影响 audit，并明确异步未提交内容的恢复边界。
- **8.3**：实现有界标签的 Prometheus Counter/Histogram/Gauge、账本刷新、审计失败/积压/缺口指标和内部 /metrics；验证重复事件不重复增量、user/agent/request/attempt/trace 不作为标签及公共端口隔离。
- **8.4**：提供 Prometheus 抓取配置与 Grafana provisioning/仪表盘；运行配置校验并实际抓取成功、错误、Fallback、unknown 和审计故障场景，检查延迟/TTFT/费用/余额/审计状态面板。
- **8.6**：完成启动、配置重载、Agent 接入、价格/限额设置、审计查询/上报、离线核对、恢复与回退文档；按文档执行保留 Redis 和 Audit Store 持久卷的重载/回退演练并记录证据。
- **8.7**：执行完整验收并记录已通过/未验证项目，运行 OpenSpec verify；包括内容审计在内的全部实现与运行验收通过后才标记任务完成，归档作为后续独立操作。

## WARNING：设计差异与覆盖边界

- 插件依赖和启停已实现；大部分请求阶段仍由 `lua/gateway.lua` 直接编排，通用 `plugin_manager.run` 当前主要用于 header hook。任务 1.3 需完成可扩展阶段调度并验证独立运行开关。
- TLS 的 SNI/证书验证配置已写入，HTTP Mock 已验证凭证/Host/模型切换；尚无 HTTPS Mock 证书正反例，也未连接真实收费模型。
- 审计工具/执行摘要限制为 100 项并显式标记截断，完整事件可按 cursor 分页；未提供单独的工具执行分页接口。
- 调用结束后的 Lua 内存队列不是持久事件队列。worker 丢失最终数据时能标记缺口并保留预占，不能恢复未落盘响应正文。
- 所有 shell 重载/回退说明已提供，但持久卷回退演练、审计吞吐开销基线、所有插件运行组合仍未完成验收。
- 计量日志不会携带正文；Nginx 原生诊断仍输出自身请求路径等诊断信息，部署时还需按实际入口约束敏感查询参数。
- 本地执行了 Linux 容器测试；已创建 GitHub Actions 工作流，尚未在远程 CI 服务运行。

## 后续执行顺序

先完成严格截止和 plugin 阶段调度，再补 worker/磁盘/队列故障注入与配置回退演练；在镜像可达环境运行监控和开销测量。按 tasks.md 逐项验收后重新执行 OpenSpec verify，最后另行归档。
