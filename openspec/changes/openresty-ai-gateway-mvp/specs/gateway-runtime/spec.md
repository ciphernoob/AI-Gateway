## Purpose

为轻量级 AI Gateway 提供可复现的运行、配置、插件扩展和故障模拟环境，使各能力可以按阶段独立验证，并保证错误配置不会进入实际流量处理路径。

## ADDED Requirements

### Requirement: 可复现的本地环境
系统 SHALL 提供容器启动入口，包含网关、独立 Redis、两个 Mock Provider、启用审计时的内部 Audit Store 及 Mock Agent 示例，以及可选监控服务；不依赖真实模型凭证即可验证核心链路。

#### Scenario: 启动测试环境
- **WHEN** 开发者在满足文档前置条件的 Linux 容器环境启动测试栈
- **THEN** 两个独立后端可达，网关存活检查返回 200，就绪检查在配置、Redis 和已启用的 Audit Store 可用时返回 200

#### Scenario: Redis 不可用
- **WHEN** Redis 断开且金额预算启用
- **THEN** 就绪检查返回 503，存活检查仍返回 200，付费调用被拒绝且不触达 Provider

#### Scenario: 审计依赖不可用
- **WHEN** 审计启用且 Audit Store 无法接受写入
- **THEN** 就绪检查返回 503，新模型请求不能绕过审计开始证据；仅关闭 metrics 插件不影响审计运行

### Requirement: 配置校验与插件依赖
系统 SHALL 在接流量前校验 YAML、模型引用、凭证环境变量、价格、限额、时区、插件依赖、尝试上限，以及审计模式、采样率、保留期、正文/队列限额与接口 scopes；无效重载 SHALL 保留旧的有效配置。可选插件可分别启停，鉴权是公开业务接口的强制依赖。

#### Scenario: 错误配置不生效
- **WHEN** 配置存在重复 Key 身份、悬空模型引用、负数价格或缺失必需凭证
- **THEN** 首次启动失败并指出配置字段；已有实例重载时保持上一个有效配置且不输出凭证值

#### Scenario: 插件依赖受约束
- **WHEN** User Quota 或 Budget Engine 启用而 Usage Collector 被关闭
- **THEN** 配置校验失败；单独关闭可观测插件不影响鉴权和代理正确性

### Requirement: 可控故障模拟
测试环境 SHALL 支持正常 JSON、分块 SSE、工具调用、429、500、连接拒绝、响应头超时、中途断流、缺失 usage 与畸形 usage，并记录实际调用次数及输入用于断言。

#### Scenario: 重现流式中断
- **WHEN** 测试指定在已输出一个内容块后断开连接
- **THEN** Mock 不发送最终 usage 或成功结束标记，测试能够判断网关是否发生了不允许的重试
