# 管理控制台验收报告

日期：2026-09-25。变更：`gateway-admin-console`。实现、容器运行及验收已完成，未执行归档；原 `openresty-ai-gateway-mvp` 的未完成任务保持原状态。

## 验证结论

| 维度 | 结果 |
|---|---|
| 完整性 | 10/10 实施任务，4 项能力，7 条要求、14 个场景均有实现与验收依据 |
| 正确性 | Go/Python/Vue 测试、真实 OpenResty 调用、浏览器流程及发布故障验收通过 |
| 一致性 | Go 标准 HTTP + Vue 同源 HTTPS、加密管理存储、内部控制代理、原生 Nginx 代理和独立账本符合设计 |

未发现阻断本变更交付的未实现要求。部署边界见下文；结论不代表原 MVP 的其他待办已完成。

## 运行证据

- 开发服务：`https://localhost:8443` 返回 200，响应包含构建后的 Vue 资源；OpenResty `http://localhost:8080/readyz` 返回 ok。正常开发项目的登录、网关及 Redis 健康、用量查询和退出冒烟检查通过。
- Go 8 项测试通过，`go vet ./...` 通过。覆盖认证、Cookie 属性、CSRF、会话过期/退出、登录限速、AES-GCM 篡改/替换拒绝、草稿冲突、一次性 Key、字段白名单、日志轮转、过期与未知用量、控制接口认证及路径限制。
- Vue 3 项测试通过；TypeScript 检查和 Vite 生产构建通过。
- Chrome 2 组真实浏览器端到端测试通过：登录 → 新建后端 Key → 新建模型入口 → 新建用户并签发 Key → 校验和发布 → JSON/SSE 调用 → 用量/审计查询 → 用户禁用 → 日志查询；另验证管理权限、CSRF 与非法配置发布。
- 最终原有网关验收：30 项 Python 测试、Lua 检查（含关联字段凭证脱敏）、8 组原有故障探针和 6 组额度切换验收全部通过。
- 管理发布专项 9 组通过：轮换新旧 Key 脱敏；Token 零额度仍可调用及 Key 签发/撤销；额度切换日志与未知用量；SSE 跨发布完成并拒绝并发发布；Audit Store 停机；Redis 停机；重启恢复发布日志；Nginx 校验失败恢复；回滚保留账本。
- 缺少 TLS 证书时交付二进制退出，未降级为 HTTP。
- 审计数据库仅保存脱敏版本引用；单元测试验证数据库逻辑导出不含新密钥、旧实现迁移清除重复明文集合、保留期及重启恢复。

运行日志位于本机忽略目录 `test-results/`：`admin-browser.log`、`admin-faults.log`、`admin-regression.log`、`admin-development-deploy.log`。浏览器截图为 `admin-models.png`，不含密钥值。这些文件不是版本库交付依赖。

## 要求与实现映射

| 要求 | 主要实现 | 验收 |
|---|---|---|
| 管理入口与会话 | `admin/main.go`、`admin/http.go` | Go 会话/CSRF/限速；Chrome 权限测试；8443 与缺证书启动检查 |
| 模型与用户管理 | `admin/resources.go`、Vue Console、配置编译器 | Chrome 完整表单流程；冲突、禁用、Key 撤销与零 Token 额度探针 |
| 凭证和版本持久化 | `admin/store.go`、Audit Store 版本引用 | AES-GCM 测试、轮换内容脱敏、重启及旧数据迁移 |
| 原子发布与恢复 | `admin/publication.go`、`admin/agent.go` | 并发、SSE、依赖停机、无效 Nginx、恢复及回滚探针 |
| 账本查询 | `admin/observation.go`、现有 Redis 账本 | 未知/过期用量测试、Redis 停机、回滚前后计数对比 |
| 日志与管理员审计 | Go 轮转事件文件、Audit Store 管理查询、Vue 详情 | 筛选/读取、轮转、正文排除、凭证脱敏；原有截断/断流/工具事件/权限测试 |
| 模型独立路径 | `nginx/nginx.conf`、`lua/gateway.lua` | JSON/SSE 成功、model 冲突 400、未知入口 404、旧统一接口回归 |

## 部署边界

- 当前本机使用开发自签证书，正式环境按运行文档挂载组织签发证书。已验证 Windows localhost HTTPS；其他局域网设备穿过 Windows WSL2 NAT 的连接未作为本次验收环境配置，需主机端口转发/防火墙或镜像网络。
- 管理端只支持单实例、单管理员；Token 额度仍为统计模式，不包含硬 Token 限额或智能路由。
- 原网关异步结算在 worker 意外退出时的恢复边界未扩大为强持久事件保证；本次验证正常平滑发布不主动中断 SSE、回滚不重置账本。
- 金额统计来自 Redis，不从审计数据重算。内容到期后显示过期；未知用量仍为 unknown，不替换为零。
- 管理版本和私密快照为回滚保留；备份必须同时包含主密钥、数据库、私密运行卷及原账本/审计卷。
