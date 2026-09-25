## Context

已有 OpenResty 原生代理、Redis 账本和 Python SQLite Audit Store，配置由 Python 校验器生成运行 JSON，worker 启动读取。管理端为独立扩展，原 MVP 的 admin UI 排除条款只约束原变更。

## Goals / Non-Goals

**Goals:** 单实例 HTTPS 管理、完整表单操作、可靠配置发布及跨用户可观测。

**Non-Goals:** 多集群、RBAC、硬 Token 限额、智能路由和协议转换。

## Decisions

- Go 标准 HTTP + SQLite、AES-GCM、服务端会话；Vue 3/TypeScript/Element Plus 构建嵌入。同源避免跨域凭证。HTTPS 强制配置证书。
- Go 保存配置文档及加密凭证版本，首次导入 YAML。通过 stdin 调用现有 Python 校验器；不重复实现网关校验，不使用 Shell 拼接。
- 运行快照采用不可变 revision + 原子 active 文件。内部 Go 控制代理与 Nginx 同容器，拥有重载能力，仅允许固定操作；避免 Docker Socket 权限。
- 发布记录先持久化；Audit Store 持久化脱敏集合的私密版本引用后才允许启用快照，密钥明文不复制到审计数据库。验证 Nginx、新 worker revision 和依赖，失败回滚；管理员重启按控制代理实际状态恢复。
- 配置更新仍为 Nginx graceful reload，旧请求绑定旧 worker/config。Lua body/log 阶段不增加 Redis cosocket，原有异步结算恢复限制不改变。
- 管理员通过独立内网认证查询 Audit Store；用户接口继续 owner 校验。用量直接读取 Redis 原子快照；运行日志写入轮转文件，正文只在 Audit Store。
- 网关用户新增 disabled；provider disabled 被候选过滤，若模型无启用候选则拒绝发布。Key 撤销、状态变化纳入版本，回滚前显式差异展示。
- 草稿资源 API 以文档 revision 乐观并发；版本/操作日志游标或分页有硬上限。密钥表单只接收值，返回 configured/reference。

## Risks / Trade-offs

- [运行快照仍有明文凭证] → 私密卷、受限权限，不进入管理响应或日志；数据库密钥独立保存。
- [重载并非跨服务事务] → 先扩大脱敏集合，再替换配置，持久发布状态和回滚；失败恢复也单独显示。
- [SQLite 单实例] → 明确仅一个管理实例，事务及进程锁串行发布。
- [缺失 usage] → 继续 unknown/pending，不从审计推算，不自动释放未知费用。

## Migration Plan

通过独立 Compose override 启用管理模式，复用原 Redis/Audit 卷，导入一次现有配置。证书、管理员密码、加密主密钥与内部令牌由部署生成。退出管理模式前导出当前私密运行配置备份，保留数据卷；不自动重写原 YAML/.env。
