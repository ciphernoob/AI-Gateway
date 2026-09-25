## Purpose

为所有业务接口提供统一的可信身份边界，将 Gateway 签发的 API Key 映射至用户和可选 Agent，使金额预算、用量记录与用户查询遵循同一身份来源，防止客户端冒用归属。

## ADDED Requirements

### Requirement: Gateway API Key 认证
系统 SHALL 使用 `Authorization: Bearer <gateway-key>` 认证 Chat Completions、用量查询和审计接口。Key 与 user_id、可选 agent_id、scopes 的关系由静态配置维护；Chat Completions 要求 chat:write，用量查询要求 usage:read，审计查询要求 audit:read，工具事件上报要求 audit:write 和绑定 agent_id。缺失、无效或禁用 Key 返回 401；有效 Key 缺少权限返回 403，均不得调用上游或产生模型用量。

#### Scenario: 有效用户请求
- **WHEN** user_001 的有效 Key 调用业务接口
- **THEN** 请求上下文与所有计量事件的 user_id 均为 user_001

#### Scenario: 无效 Key
- **WHEN** 调用方未携带 Key 或携带未知、禁用 Key
- **THEN** 返回统一格式的 401，Provider 调用次数与账本计数不变

#### Scenario: 审计访问权限
- **WHEN** 有效 Key 只具备 chat:write 和 usage:read 而调用审计查询或上报
- **THEN** 返回 403，不读取正文、不写工具事件，也不能通过指定 user_id 或 agent_id 提升权限

### Requirement: 禁止客户端替换可信身份
系统 MUST 不使用请求体、查询参数或自定义头中的 user_id、agent_id 作为鉴权或计费主体；Agent 预算主体 SHALL 来源于 Key 配置，同用户多个 Key 的用户用量 SHALL 合并。

#### Scenario: 伪造归属
- **WHEN** user_001 的 Key 携带 user_002 或其他 agent_id
- **THEN** 账本仍归属 user_001 及 Key 绑定的 Agent；伪造值不影响预算或查询授权

#### Scenario: 用户轮换 Key
- **WHEN** 同用户的两个有效 Key 分别产生用量
- **THEN** 两次用量归入同一个用户的日/月计数，禁用旧 Key 后旧 Key 不再可调用

### Requirement: 凭证隔离
系统 SHALL 从独立配置引用 Provider 凭证，MUST 不转发 Gateway Key、不记录完整任何 Key，也不得在查询或错误响应中返回内部凭证。

#### Scenario: 检查实际上游请求
- **WHEN** 请求经认证并代理到 Provider
- **THEN** Provider 收到其自己的认证凭证，无法从请求头获得 Gateway Key，日志和响应中不含完整凭证
