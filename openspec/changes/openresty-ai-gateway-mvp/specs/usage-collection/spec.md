## Purpose

为预算、用户配额与可观测性提供一次采集、多处使用的标准化用量事实。明确区分 Provider 精确数据、估算数据和未知数据，并在重试、断流与异步重复投递时维持可核对的尝试级记录。

## ADDED Requirements

### Requirement: 标准尝试事件
系统 SHALL 为每个已开始的后端尝试生成 Usage Event，包含 request_id、attempt_id、user_id、可选 agent_id、逻辑与实际模型、Provider、输入/输出/总 Token、usage_source、usage_status、调用 status、时间及价格版本。MVP 精确统计 SHALL 优先使用有效 Provider usage，输入输出之外的缓存和推理详情不得被重复相加。

审计启用时事件 SHALL 关联可信 trace_id，但不包含消息正文或工具执行结果；审计正文超限/存储失败 SHALL 不阻止已经返回的有效 usage 进入计量，Agent 工具上报不得生成模型计量事件。

#### Scenario: Provider 精确用量
- **WHEN** 后端返回 prompt_tokens=1200、completion_tokens=500、total_tokens=1700
- **THEN** 事件保留三个数值，usage_source=provider、usage_status=known；所有消费者引用同一 attempt_id 的这组用量

#### Scenario: 审计截断与计量隔离
- **WHEN** 审计正文已达上限，但流末尾仍返回有效 usage
- **THEN** 审计显示 truncated，Usage Collector 仍正常采集最终 Token 并只结算一次

### Requirement: 分块流式解析
系统 SHALL 为声明支持的流式后端请求 include_usage，正确解析跨网络块的 SSE 事件与最终统计；用量采集内存有界且不得改变已转发内容。

#### Scenario: usage 跨块
- **WHEN** JSON/SSE 行和多字节字符被拆分在多个网络块，前置 chunk 的 usage 为 null
- **THEN** 仅最终有效 usage 形成精确统计，同一累计 usage 重复出现不导致加倍

#### Scenario: 不支持 usage 的兼容后端
- **WHEN** 配置声明后端不支持 stream_options.include_usage
- **THEN** 不注入不兼容参数，流正常转发，最终缺失用量被明确标记未知

### Requirement: 未知与零严格区分
系统 MUST 将缺失、畸形、负值、超出安全整数范围或不一致的 usage 标记为 unknown，Token 字段为 null；只有证实请求从未发送时才允许 system 来源的精确零。估算值 SHALL 与精确值分离，MVP 不将估算加入精确 Token 计数。

#### Scenario: 断流缺失统计
- **WHEN** 请求已发送但在最终 usage 前断开
- **THEN** 事件为 usage_source=unknown、usage_status=unknown、status=interrupted，Token 为 null，预算预占保留且用户未知尝试数增加

#### Scenario: 从未发送的连接失败
- **WHEN** 证据确认上游发送字节为零且未收到任何上游响应
- **THEN** 事件为 usage_source=system、usage_status=known、Token 全部为零、status=failed，可释放该尝试的金额预占

### Requirement: 有界异步消费与幂等重试
系统 SHALL 在允许网络 I/O 的阶段消费事件；重复投递同一 attempt_id SHALL 不重复计量，结算暂时失败 SHALL 有限重试并暴露状态。未持久化事件在 worker 异常退出时不保证精确恢复，系统 MUST 保留调用前已持久化的尝试记录以暴露该缺口。

#### Scenario: 重复与短暂断连
- **WHEN** 一条事件重复投递三次且 Redis 短暂断开后恢复
- **THEN** 成功消费后用户用量和金额只结算一次，失败可观测且不会阻塞已经开始的客户端流

#### Scenario: worker 丢失最终事件
- **WHEN** 调用开始记录已持久化但 worker 在最终结算前退出
- **THEN** 恢复扫描将未完成尝试转为未知待核对，未用零值伪装成功结算
