## Why

当前网关同时维护金额预算与 Token 用量，配置和运行链路包含价格、金额预占及结算，但当前产品只需要按用户和模型忠实统计后端返回的 Token。移除金额语义可以避免网关以本地价格推导费用，并让用量数据清楚表达其唯一来源。

## What Changes

- **BREAKING**：删除 Budget Engine、模型价格字段、全局/模型/Agent 金额额度、金额预占与金额结算，以及管理端金额概览和编辑入口。
- Token 账本调整为 `user_id × logical_model × day/month`，分别保存输入、输出、总 Token。
- 只将后端明确返回且通过一致性校验的 usage 计入 Token；缺失或非法 usage 不估算、不写零值，并记录为对应用户和模型的未知尝试。
- Fallback 的每次后端尝试独立记账，确保同一请求使用过的每个模型都按其自身返回 usage 统计。
- 用户用量接口和管理页面支持按用户、模型及时间窗口查询，汇总值由各模型账本求和。
- 保留用户日/月 Token 配额作为展示性统计额度，不新增硬配额拦截。

## Capabilities

### New Capabilities

- `provider-token-accounting`：规定按用户和逻辑模型忠实采集、校验、幂等结算及查询 Provider Token usage 的行为。
- `token-usage-administration`：规定管理配置、概览和用量页面仅呈现 Token 统计并移除全部金额字段的行为。

### Modified Capabilities


## Impact

影响 Lua 请求与异步结算链路、Redis 原子脚本及键结构、配置校验和示例、Go 管理 API、Vue 表单与概览、审计 usage 元数据、Prometheus 指标、测试和运维文档。现有 Redis 金额账本不再读取或更新；既有用户总量键需要在查询兼容期内与新的按模型键明确区分。
