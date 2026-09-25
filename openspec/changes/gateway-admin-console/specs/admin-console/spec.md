## Purpose

为单套网关提供中文管理入口，将模型、后端凭证、用户与配置操作集中到经过身份验证的 HTTPS 页面，同时避免管理凭证与业务用户凭证混用。

## ADDED Requirements

### Requirement: 管理入口与会话
系统 SHALL 在 8443 提供 HTTPS 和中文 Vue 界面，使用单管理员密码认证、服务端会话、CSRF 防护及登录限速，缺少 TLS 材料 MUST 拒绝启动。
#### Scenario: 非管理员请求
- **WHEN** 未登录、会话过期或业务 Key 请求管理 API
- **THEN** 返回 401，不返回管理数据
#### Scenario: 写操作保护
- **WHEN** 已登录但缺少有效 CSRF 的写操作到达
- **THEN** 返回 403 且状态不变

### Requirement: 模型与用户管理
管理员 SHALL 通过表单管理模型、价格、能力、最多两个候选、后端 Key、用户状态、Gateway Key 和统计额度；凭证 SHALL 不回显，新 Gateway Key 仅签发时展示。
#### Scenario: 配置草稿
- **WHEN** 管理员保存模型或禁用用户
- **THEN** 页面标注待发布，当前调用行为不变，发布后新请求使用新配置
#### Scenario: 配额语义
- **WHEN** 用户 Token 超额但金额预算足够
- **THEN** 仍允许调用，界面标明 Token 仅统计而金额受预算限制
