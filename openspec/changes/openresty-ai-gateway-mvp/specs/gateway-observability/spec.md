## Purpose

使运营者能够观察网关流量、延迟、Token、费用和故障恢复效果，并区分已确认用量与待核对记录。同时定义独立 Audit & Trace 模块，记录请求与回复内容、关联模型调用和工具执行事件，提供有权限边界、有完整性状态的审计查询。

## ADDED Requirements

### Requirement: 结构化脱敏日志
系统 SHALL 输出请求级与尝试级 JSON 运行日志，关联 request_id、attempt_id、trace_id、可信身份、模型、Provider、状态、时延、用量来源、费用及结算状态；运行日志 MUST 不记录完整 API Key、消息正文或工具参数。需要保存的正文 SHALL 进入下述独立审计存储；任何存储通道均不得保留网关或 Provider 认证凭证。

#### Scenario: 主备请求追踪
- **WHEN** 一个请求先连接失败再成功
- **THEN** 可按 request_id 找到两条独立尝试和一次请求结果，无重复请求成功计数且不泄露凭证或提示词

### Requirement: 有界聚合指标
系统 SHALL 提供请求率/错误率、延迟直方图、TTFT、输入输出 Token、已结算与未知费用状态、Fallback 次数/结果、金额余额、未结算数量及结算失败指标。标签 SHALL 限于配置允许的模型、Provider、预算范围和有限状态，不含 user_id、agent_id、request_id、attempt_id 或自由文本。

#### Scenario: TTFT 语义
- **WHEN** SSE 先收到 HTTP 头和仅 role 的 chunk，之后才有非空内容或工具参数
- **THEN** TTFT 记录首次实际内容或工具参数的时间，非生成响应没有伪造的 TTFT

#### Scenario: 多用户聚合
- **WHEN** 大量不同用户调用同一组模型
- **THEN** 指标序列数不随用户或请求 ID 数增长，用户明细通过认证查询或脱敏日志查看

### Requirement: 监控隔离与仪表盘
系统 SHALL 仅向内部监控网络提供 `/metrics`，Grafana SHALL 预置请求率、成功率、P50/P95、TTFT、Token、费用、Fallback 与待核对面板；金额图表不得把多个预算维度当多笔费用相加。

#### Scenario: 监控验收
- **WHEN** 执行成功、错误、Fallback 和未知 usage 场景后由 Prometheus 抓取
- **THEN** 相应指标和 Grafana 面板出现数据，公共业务监听端口无法匿名获取内部指标

### Requirement: 验收结果可复现
项目 SHALL 提供自动化验收入口，覆盖八项能力的正常、并发和故障边界，区分已经运行通过与因环境缺失未运行的检查。

#### Scenario: 完整 MVP 验收
- **WHEN** 开发者执行文档化的测试命令
- **THEN** 得到可追溯到规格场景的通过或失败结果，包括双后端、SSE、身份隔离、重试边界、原子预算、幂等计数、周期边界、异常恢复及调用审计

### Requirement: 独立调用内容审计
系统 SHALL 提供独立 Audit & Trace 模块，默认 full 模式、采样率 1，保存每次通过认证且有效的模型请求正文（含 messages、系统消息、模型参数、tools 定义及回传工具结果）、各次尝试的上游请求快照、LLM 回复、tool_calls 及最终客户端响应的关联。记录 SHALL 包含可信身份、request_id、attempt_id、模型、Provider、状态、时间和策略版本；不得用工具调用指令推断执行成功。认证失败或无效请求仅记录可获得的安全元数据和拒绝原因，不存未验证正文。

#### Scenario: 普通调用内容可核对
- **WHEN** 用户完成含系统提示、工具定义的普通模型调用
- **THEN** 本人可查询脱敏后的请求、对应 LLM 返回内容和用量关联，并区分逻辑请求与适配后上游请求，常规运行日志不含这些正文

#### Scenario: Fallback 尝试分离
- **WHEN** 主尝试连接失败后备用成功
- **THEN** 两次尝试各有独立审计状态；未收到回复的主尝试不复制备用回复，客户端最终结果指向备用尝试

### Requirement: 流式内容重建与完整性
系统 SHALL 按 attempt_id、choice index 和 tool call index 有序重建 SSE 的文本与工具参数，并保留结束原因和采集范围。采集上限、断流、解析失败、尚未提交及策略不采集 SHALL 在响应完整性和正文状态中明确表示；不得把缺失内容表示成空回复或 complete。审计处理 MUST 不为了等待完整生成而缓冲客户端输出。

#### Scenario: 分片工具参数
- **WHEN** 两个工具调用的名称和 JSON 参数交错分片返回
- **THEN** 审计按各自 index/id 重组，工具调用之间不混合，客户端仍逐块收到数据

#### Scenario: 截断或中断
- **WHEN** SSE 中途断开，或正文超出配置上限
- **THEN** 查询返回已捕获范围和 interrupted/truncated 状态，不伪造完整回复、成功结束或完整工具参数 JSON

### Requirement: 多轮追踪与归属校验
系统 SHALL 为模型请求生成 request_id/attempt_id 和未提供时的 trace_id；复用 X-Trace-ID 及 X-Parent-Request-ID 时 SHALL 校验现有记录属于同一认证用户，响应返回 X-Trace-ID。conversation_id 仅为客户端关联提示，不是授权依据。未经验证的关联 MUST 不合并到他人链路。

#### Scenario: 多轮 Agent 任务
- **WHEN** Agent 使用上一轮返回的 trace_id 和 parent request_id 提交工具结果并再次调用模型
- **THEN** 审计可关联两轮请求、各自尝试和工具调用，且仍分别计量

#### Scenario: 伪造追踪标识
- **WHEN** 用户提交不存在或属于其他用户的 trace/parent request 标识
- **THEN** 返回统一 404 且不触达 Provider、不泄露记录是否属于其他用户；同用户但父请求与 trace 不匹配返回 400

### Requirement: Agent 工具执行上报
系统 SHALL 提供 `POST /v1/audit/events` 接收 tool.started、tool.completed、tool.failed，包含 event_id、tool_execution_id、父 request_id/attempt_id、tool_call_id、发生时间及适用的参数、结果或异常。只有持有 audit:write scope 且绑定可信 agent_id 的 Key 可上报，系统 SHALL 校验父调用的 user_id/agent_id 以及工具指令归属；事件来源固定为 agent_reported，接收时间由网关记录。网关自身采集为 gateway_observed，两者不可相互覆盖。

#### Scenario: 工具执行形成链路
- **WHEN** Agent 为已记录的 tool_call 上报开始与完成事件
- **THEN** 查询展示工具名、参数、结果及上报时间/接收时间，可关联后续 role=tool 消息，明确执行信息来自 Agent

#### Scenario: 未上报执行
- **WHEN** 只观察到 LLM tool_calls 或下一轮 role=tool 消息，没有工具执行上报
- **THEN** 查询只表示“生成了指令”或“收到了客户端提供的结果”，执行状态为未上报，不虚构耗时或成功事实

#### Scenario: 非法上报
- **WHEN** 缺少 scope、伪造主体/来源、引用他人父请求或不存在的工具指令
- **THEN** 缺少权限返回 403，越权/未知父请求返回统一 404，非法主体/来源或已完成父请求中不存在的工具指令返回 400；尚未完成入库的同用户工具指令返回可重试 409，不写入事件

### Requirement: 幂等事件与证据追加
系统 SHALL 以认证主体和 event_id 幂等接收事件；重复同内容返回既有结果，冲突内容返回 409。工具重试 SHALL 使用不同 tool_execution_id；乱序事件保留接收顺序与原始时间，并显示缺失阶段。已接收事实不得通过公开接口覆盖；修正只能追加关联事件，历史正文仍受保留期约束。Agent 事件 MUST 不改变用量、预算或网关观察到的返回状态。

#### Scenario: 重复与乱序
- **WHEN** 完成事件先于开始事件到达且完成事件重传三次
- **THEN** 完成事件只保存一份，开始缺失时标记缺口，补到开始事件后更新派生视图并保留原始接收顺序

#### Scenario: 上报内容不能影响费用
- **WHEN** Agent 在工具结果中声称不同 Token 数或模型执行状态
- **THEN** 原 Usage Event、账本及 gateway_observed 事件不被覆盖，也不触发重复结算

### Requirement: 本人审计查询与查询留痕
系统 SHALL 提供 `GET /v1/audit/requests`、`GET /v1/audit/requests/{request_id}`、`GET /v1/audit/traces/{trace_id}`，要求有效 Gateway Key 和 audit:read scope，强制限制为本人。列表 SHALL 有界分页、允许时间/模型/状态过滤且默认不返回正文；详情经 `include_content=true` 返回策略允许的正文及保存状态。查询访问 SHALL 记录操作者、目标、时间和结果，不重复记录所读取正文。

#### Scenario: 审计详情查询
- **WHEN** 有权限用户查询自己的请求详情或 trace
- **THEN** 返回请求、逐次尝试、工具事件、采集来源及完整性状态；正文只在显式请求且尚保留时返回

#### Scenario: 审计查询隔离
- **WHEN** 用户猜测他人的 request_id/trace_id，或尝试传入其他 user_id
- **THEN** 他人资源与不存在资源均返回 404，指定 user_id 参数返回 400；列表始终按认证用户过滤，游标不能跨用户复用

### Requirement: 内容策略与生命周期
系统 SHALL 提供 full/metadata_only/off 模式、确定性采样、正文大小上限、字段脱敏与独立保留期配置，默认 full/1；客户端不得降低服务端审计策略。默认正文保留 7 天、元数据 30 天，查询 SHALL 区分 captured/redacted/truncated/not_captured/expired。认证头、Cookie 与已知 Gateway/Provider 密钥 MUST 在持久化和摘要计算前移除或替换；正文内容不能进入 Prometheus 标签或 Redis 账本。

#### Scenario: 正文到期
- **WHEN** 请求正文到达保留期但元数据尚未到期
- **THEN** 正文从可查询存储删除，详情仍显示元数据与 expired，后续重放不得复活已到期内容，预算和 Token 账本不受影响

#### Scenario: 不采集和脱敏
- **WHEN** 策略为 metadata_only、样本未命中或字段包含配置指定的敏感信息
- **THEN** 记录说明正文未采集或已脱敏，已知密钥不出现在正文/摘要输入/运行日志中，客户端真实请求与回复不被审计脱敏改写

### Requirement: 审计持久化与故障边界
审计启用时系统 SHALL 在转发前持久化请求及尝试开始证据，失败默认返回 503/audit_unavailable 而不调用 Provider；已开始的响应不因审计结尾写入失败而重放或伪造成功记录。最终内容异步持久化，接收工具事件的成功响应 SHALL 仅在持久化提交后返回。系统 SHALL 暴露写入失败、积压、未完成和内容截断指标，并恢复已落盘事件；丢失的未提交正文只能标记 incomplete。

#### Scenario: 审计存储不可用
- **WHEN** 新模型请求开始时 Audit Store 不可用或容量不足
- **THEN** 请求返回 503，Provider 未被调用，已预占且证实未发送的金额被释放或由恢复流程处理

#### Scenario: worker 中途退出
- **WHEN** 开始证据已落盘而 worker 在最终正文持久化前退出
- **THEN** 超过请求恢复期限后可查到 incomplete 记录；恢复流程不把它标记 complete，也不影响金额未知预占策略
