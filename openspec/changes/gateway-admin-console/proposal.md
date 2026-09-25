## Why

现有网关只能通过配置文件和命令发布，缺少统一的模型、密钥、用户及审计管理入口。增加独立管理端，让管理员通过 HTTPS 完成配置和排障。

## What Changes

- Go HTTPS 8443 管理 API、单管理员会话和 Vue 中文界面。
- SQLite 加密凭证、版本化草稿、校验发布、平滑重载、恢复及回滚。
- 模型独立 URL、后端主备与额度耗尽开关、用户及 Gateway Key 管理。
- 跨用户审计、Redis 用量/预算查询、结构化运行日志和操作审计。
- 保留文件部署和现有统一调用；不加入硬 Token 配额、智能路由或多集群。

## Capabilities

### New Capabilities
- `admin-console`: HTTPS 管理界面、身份验证与管理资源。
- `admin-publication`: 配置版本发布、密钥保护与恢复。
- `admin-observation`: 管理员日志、审计和用量查询。
- `model-endpoints`: 模型独立路径及兼容行为。

### Modified Capabilities

无。既有 MVP 尚未归档，本变更通过新增能力描述扩展，保留其未完成任务。

## Impact

新增 Go/Vue 构建及管理容器；扩展 OpenResty 路由、Python 配置校验器、Audit Store 内部接口、Compose 与验收测试。管理数据、日志和证书使用独立卷或只读挂载，Redis/Audit 数据不迁移清空。
