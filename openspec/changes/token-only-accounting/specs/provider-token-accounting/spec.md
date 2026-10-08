## Purpose

定义网关如何按可信用户和逻辑模型忠实记录后端返回的 Token usage，并在重试、并发、缺失数据和依赖故障下保持可解释、可去重的统计结果。

## ADDED Requirements

### Requirement: Provider usage 是唯一 Token 数值来源
系统 SHALL 只将后端响应中通过校验的 `prompt_tokens`、`completion_tokens` 和 `total_tokens` 计入账本，且 MUST 保持后端给出的三个基础数值，不得通过请求内容、本地 tokenizer、输出长度、模型价格或默认值估算 Token。

#### Scenario: JSON 响应返回有效 usage
- **WHEN** 后端返回非负安全整数且 `prompt_tokens + completion_tokens = total_tokens`
- **THEN** 系统将这三个原始数值以 `usage_source=provider` 记录到该次尝试

#### Scenario: SSE 最终事件返回有效 usage
- **WHEN** 流式响应的最终 usage 事件通过相同校验
- **THEN** 系统忠实记录该事件的三个基础 Token 数值

#### Scenario: usage 缺失或非法
- **WHEN** 后端未返回 usage、流在 usage 到达前中断，或三个数值无效或不一致
- **THEN** 系统不得写入估算值或零值，并将该尝试标记为未知用量

#### Scenario: 请求未发送至后端
- **WHEN** 某次候选尝试在连接后端前终止
- **THEN** 系统不得为该尝试写入 Token 或未知用量

### Requirement: 按用户和逻辑模型分账
系统 SHALL 按可信 Gateway Key 得到的 `user_id`、请求选择的逻辑模型以及尝试开始时所属的 Asia/Shanghai 日/月窗口维护输入、输出、总 Token、待结算尝试数和未知尝试数。

#### Scenario: 用户使用一个模型
- **WHEN** 用户对逻辑模型 `coding` 的一次尝试返回有效 usage
- **THEN** 日账本和月账本仅增加该用户 `coding` 模型下的对应 Token

#### Scenario: 同一用户使用多个模型
- **WHEN** 同一用户分别使用 `coding` 和 `balanced`
- **THEN** 查询可分别返回两个模型的统计，并可从模型统计求得用户汇总

#### Scenario: 请求伪造身份
- **WHEN** 请求正文携带与 Gateway Key 不同的用户标识
- **THEN** 统计仍归属 Gateway Key 映射的可信用户

### Requirement: 每次后端尝试独立且幂等结算
系统 SHALL 以唯一 `attempt_id` 对每次实际后端尝试结算一次；Fallback 跨模型时，各尝试 MUST 归入其实际使用的逻辑模型统计，重复结算 MUST 不重复增加 Token。

#### Scenario: 同模型不同 Key Fallback
- **WHEN** 首个 Key 返回用量后触发备用 Key，且备用尝试也返回用量
- **THEN** 两次 Provider usage 分别结算到同一用户和逻辑模型

#### Scenario: 跨模型 Fallback
- **WHEN** 两个候选对应不同逻辑统计模型并分别返回 usage
- **THEN** 每次 usage 归入该尝试实际选择的模型，不合并到错误模型

#### Scenario: 并发重复投递结算事件
- **WHEN** 多个 worker 并发提交相同 `attempt_id` 和相同 usage
- **THEN** 只有一次更新账本，其余返回幂等重复结果

#### Scenario: 冲突的重复事件
- **WHEN** 相同 `attempt_id` 再次提交不同 usage
- **THEN** 系统拒绝冲突事件并保持首次已结算数值

### Requirement: Token 统计不包含金额语义
系统 SHALL 不要求价格、币种或金额额度，不执行金额预占、金额结算或金额超限拒绝，也不得输出由 Token 推导的费用。

#### Scenario: 高用量请求
- **WHEN** 用户请求返回有效 usage 且不存在 Token 硬限额拦截
- **THEN** 系统记录 Token，但不会因任何金额余额拒绝请求

### Requirement: 依赖故障保持显式状态
系统 SHALL 在 Redis 不可用或异步结算无法完成时保留可恢复的未结算状态，并且查询 MUST 返回依赖不可用或 pending/unknown，不得把缺失统计显示为零。

#### Scenario: Redis 在请求开始时不可用
- **WHEN** 网关无法创建尝试账本
- **THEN** 请求返回明确的计量依赖错误且不访问后端

#### Scenario: Redis 在响应后不可用
- **WHEN** 已观察到 Provider usage 但结算暂时失败
- **THEN** 系统重试结算并暴露未完成状态，不生成估算数据

