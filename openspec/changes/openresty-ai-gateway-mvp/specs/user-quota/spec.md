## Purpose

为不同用户提供与金额预算独立的 Token 用量统计和配额查询，保证日月窗口、身份隔离、未知数据提示和幂等记账一致，并为后续 Token 硬限额预占与拦截保留清晰的扩展边界。

## ADDED Requirements

### Requirement: 日月独立统计
系统 SHALL 按可信 user_id 同时维护每日和每月 prompt_tokens、completion_tokens、total_tokens，并单独记录未知及待结算尝试数。统计时区为 Asia/Shanghai，以每次尝试开始时刻归属窗口；历史窗口有明确保留期。

#### Scenario: 精确用量同时入账
- **WHEN** user_001 在某日完成一次已知 1700 Token 的尝试
- **THEN** 该日及所属月的三个精确计数均增加相应值，其他用户不受影响

#### Scenario: 跨日结算
- **WHEN** 尝试于 23:59 开始并在次日或次月结算
- **THEN** 用量归入开始时所属日和月，重试结算不改变归属窗口

### Requirement: 幂等且并发安全
系统 SHALL 将同一事件的日/月计数更新和消费状态一起提交；同一 attempt_id 重复消费不重复计数。未知转精确的受控补录 SHALL 只补记一次并减少相应未知计数。

#### Scenario: 并发完成与重复投递
- **WHEN** 100 个不同尝试并发提交且其中 10 个事件被重复投递
- **THEN** 日/月总数等于 100 个独立事件的和，无遗漏且不包含重复部分

#### Scenario: 未知事件核对
- **WHEN** 管理者通过受控离线工具为未知尝试提供精确用量并重复执行
- **THEN** 精确 Token 只增加一次，未知计数只减少一次，保留补录审计信息

### Requirement: 本人查询与额度展示
系统 SHALL 提供认证的 `GET /v1/usage`，返回本人当前日/月窗口、精确计数、配置限额、剩余量、未知数、待结算数、统计时区和 `enforcement=false`。剩余量为 `max(limit-known_total,0)`；未配置限额返回 null。结果 SHALL 声明异步一致性，未知或待结算存在时不得声称剩余量精确。

#### Scenario: 查询自身
- **WHEN** user_001 以有效 Key 查询，日限额为 50000 且已知用量为 1700
- **THEN** 日剩余为 48300，并返回月数据、未知/待结算状态及 enforcement=false

#### Scenario: 跨用户查询
- **WHEN** user_001 使用 user_id 查询参数试图查询 user_002
- **THEN** 请求返回 400 不支持的查询参数，不返回 user_002 的任何信息

#### Scenario: 存储不可用
- **WHEN** Redis 不可达
- **THEN** 查询返回 503，不用伪造零用量或全额剩余代替故障

### Requirement: MVP 只观察 Token 配额
系统 SHALL 接受 YAML daily_token_limit 与 monthly_token_limit，但 MUST 不因用户 Token 使用量超限而拒绝生成请求；金额预算策略仍独立生效。

#### Scenario: Token 超限但金额充足
- **WHEN** 用户已超过日 Token 配置限额而有效金额预算充足
- **THEN** 请求可继续调用，后续用量继续累加，查询剩余显示 0

#### Scenario: Token 有余但金额不足
- **WHEN** 用户 Token 剩余为正而模型金额预算不足
- **THEN** 请求由金额预算规则拒绝，错误标识 budget_exceeded 而非 Token 配额超限
