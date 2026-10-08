## Purpose

定义管理端和用户查询接口如何配置并展示纯 Token 用量，使管理员能够按用户、模型和时间窗口核对用量，同时确保金额配置与金额结果完全退出产品界面。

## ADDED Requirements

### Requirement: 配置只包含 Token 统计所需字段
系统 SHALL 从模型候选、用户、Gateway Key 和全局设置中移除价格、币种、金额预算及价格版本字段，配置校验器 MUST 拒绝遗留金额字段，发布快照 MUST 不包含金额凭证或金额策略。

#### Scenario: 发布纯 Token 配置
- **WHEN** 模型配置包含 Provider、实际模型、能力和上下文限制但不包含价格
- **THEN** 草稿校验和发布成功

#### Scenario: 提交遗留价格或预算
- **WHEN** 管理 API 或 YAML 包含价格、金额预算或金额额度字段
- **THEN** 校验返回明确错误且不发布该配置

### Requirement: 用户用量支持模型维度
用户接口和管理员接口 SHALL 返回指定日/月窗口内按逻辑模型拆分的 Token 用量，并提供由模型结果求和的用户总计；结果 MUST 区分已知 Token、pending、unknown、过期和依赖不可用。

#### Scenario: 用户查询自己的用量
- **WHEN** 已认证用户调用 `/v1/usage`
- **THEN** 只返回该用户的模型明细与用户汇总，不暴露其他用户数据

#### Scenario: 管理员按模型筛选
- **WHEN** 管理员指定 user、model 和有效时间窗口
- **THEN** 管理 API 返回该组合的日/月输入、输出、总 Token 及状态

#### Scenario: 历史窗口过期
- **WHEN** 查询超出配置保留期的窗口
- **THEN** 返回不可用状态而不是零 Token

#### Scenario: Redis 不可用
- **WHEN** 管理端无法读取 Token 账本
- **THEN** 返回明确的依赖错误且页面不保留旧统计冒充当前结果

### Requirement: 管理界面只展示 Token 计量
管理界面 SHALL 删除金额预算余额、金额单位、价格输入、费用分析和金额相关提示；概览和用户用量页面 SHALL 展示用户与模型维度的 Token 统计及未知/待结算状态。

#### Scenario: 编辑模型与用户
- **WHEN** 管理员打开模型、用户或设置表单
- **THEN** 页面不显示任何价格或金额预算字段

#### Scenario: 查看概览
- **WHEN** 管理员打开概览页面
- **THEN** 页面显示 Token 和计量健康状态，不显示预算余额或费用

### Requirement: Token 查询仍受身份与会话保护
普通用户查询 SHALL 继续使用 Gateway Key 的 `usage:read` 权限；管理员查询 SHALL 继续要求有效管理会话，且写操作的认证和 CSRF 保护保持不变。

#### Scenario: 普通用户跨用户查询
- **WHEN** 普通用户尝试指定其他用户
- **THEN** 系统拒绝跨用户访问或忽略不可信用户参数

#### Scenario: 未登录访问管理用量
- **WHEN** 请求未携带有效管理员会话
- **THEN** 管理 API 拒绝访问

