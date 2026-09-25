## Purpose

为 Agent 提供稳定的 OpenAI-compatible Chat Completions 入口，隐藏后端寻址和认证差异，同时保留普通响应、流式内容与工具调用语义，使多个兼容服务能够通过同一客户端接入。

## ADDED Requirements

### Requirement: 统一模型入口
系统 SHALL 支持 `POST /v1/chat/completions` 的 model、messages、stream 及兼容工具调用字段；根据配置将逻辑模型映射到上游模型、地址、凭证与能力。至少两个独立兼容后端 SHALL 可通过统一接口调用。

#### Scenario: 切换逻辑模型
- **WHEN** 同一客户端使用两个已注册逻辑模型分别调用
- **THEN** 请求到达各自配置的后端与实际模型，客户端只需更改 model

#### Scenario: 请求校验失败
- **WHEN** 请求 JSON 畸形、缺少必需字段、model 未注册、能力不兼容或超出配置请求大小
- **THEN** 返回统一错误；语义错误为 400、体积超限为 413，未产生上游调用和金额预占

### Requirement: JSON 与 SSE 语义保持
系统 SHALL 及时转发 JSON 和 SSE，保留工具调用参数、finish_reason 及 usage 等字段；SSE 不得等完整生成结束后才输出，亦不得拼接不同尝试的内容。响应 model 保留 Provider 返回值，逻辑模型通过请求上下文和日志追踪。

#### Scenario: 流式逐块转发
- **WHEN** 后端分时发出内容块、工具参数块与最终 usage
- **THEN** 客户端在后端完成前收到首个内容块，合并后的语义与后端一致，工具字段不丢失

#### Scenario: 普通响应
- **WHEN** 后端返回带 usage 的 Chat Completion
- **THEN** 客户端获得有效 JSON，choices、tool_calls、usage 和结束原因保持一致

### Requirement: 有界错误处理与网络约束
系统 SHALL 定义连接、读取和请求总时限，使用配置允许的上游地址并校验 HTTPS；网关生成的错误 SHALL 使用 `error.message/type/code` 结构和 request_id。已开始的 SSE 发生中断时 MUST 不伪造成功结束。

#### Scenario: 读取超时
- **WHEN** 上游已接收请求但在响应头之前超时
- **THEN** 网关在配置时限内返回 504 格式化错误，保守记为未知用量，且不重发该 POST

#### Scenario: 客户端指定地址
- **WHEN** 客户端试图用请求字段或头指定上游 URL 或认证信息
- **THEN** 转发地址及凭证仍完全由注册表决定
