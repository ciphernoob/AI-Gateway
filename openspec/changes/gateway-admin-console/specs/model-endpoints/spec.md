## Purpose

提供可直接配置给 Agent 的每模型独立调用地址，并与现有统一 Chat Completions 接口共享身份、预算、审计和主备策略，避免独立路径形成策略旁路。

## ADDED Requirements

### Requirement: 模型独立路径
系统 SHALL 支持 `/models/{name}/v1/chat/completions`，URL 选择逻辑模型，省略 body.model 时自动补齐，不一致时返回 400；统一接口行为保持不变。
#### Scenario: 独立路径调用
- **WHEN** 已认证用户通过已配置模型路径提交 JSON 或 SSE 请求
- **THEN** 转发对应候选，执行同样预算、额度切换及审计逻辑
#### Scenario: 冲突模型
- **WHEN** 请求体 model 与路径不一致或入口不存在
- **THEN** 分别返回 400 或 404，不联系后端
