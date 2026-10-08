## 1. M0 配置契约与迁移

- [x] 1.1 删除配置校验器、示例 YAML 和管理导入中的 budget、价格及价格版本字段，并以配置单元测试验证纯 Token 配置通过且遗留金额字段被拒绝
- [x] 1.2 为既有管理草稿和历史版本实现确定性去金额迁移，并以 Go 测试验证启动、发布与旧版本回滚不会重新引入金额字段

## 2. M1 Provider Token 事件

- [x] 2.1 精简 Usage Event，仅接受后端返回且满足三项一致性的 Token，删除 cost 与 system-zero 路径，并以 Lua 单元测试覆盖 JSON、SSE、缺失和非法 usage
- [x] 2.2 确保每个实际发送的 Fallback attempt 绑定可信 user_id 与逻辑统计模型，并以 Lua 测试覆盖同模型和跨模型尝试归属

## 3. M2 Redis 纯 Token 账本

- [x] 3.1 将 Redis 键改为用户×逻辑模型×日/月并删除金额 reserve/settle 操作，以真实 Redis 集成测试验证原子更新、幂等、冲突、并发和过期行为
- [x] 3.2 精简异步结算与恢复器，使未发送尝试不计量、已发送但无 Provider usage 的尝试为 unknown，并以故障测试验证 pending、恢复和依赖失败

## 4. M3 查询、管理端与可观测性

- [x] 4.1 更新 `/v1/usage` 和 Go 管理用量 API，提供用户汇总及按模型明细，并以接口测试验证身份隔离、模型筛选、过期与 Redis 故障
- [x] 4.2 删除 Go 管理 API 和 Vue 页面中的价格、预算及费用字段，更新概览为 Token 统计，并通过 Go、Vue 组件和浏览器测试
- [x] 4.3 删除金额 Prometheus 指标和日志字段，保留 Token、unknown、pending 与 attempt 关联，并通过指标及日志断言验证

## 5. M4 审计、文档与总体验收

- [x] 5.1 从 Audit Store usage 关联中删除费用数据并保留 Provider 原始 Token 与来源状态，以审计单元测试验证内容和跨用户隔离
- [x] 5.2 更新 README、管理手册和 OpenSpec 项目上下文，明确只统计 Provider usage、无金额预算及无 Token 硬拦截，并运行文档/配置一致性检查
- [x] 5.3 执行 OpenSpec strict 校验、Go/Vue/Python/Lua 测试及 Docker JSON、SSE、Fallback、故障验收，确认所有相关场景通过后再勾选本变更任务
