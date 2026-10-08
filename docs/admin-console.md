# 管理控制台

Go 后端监听 `0.0.0.0:8443` 并提供 HTTPS 和 Vue 中文页面。网关调用仍由 OpenResty 负责，管理服务故障不直接阻断已配置的模型代理。

实现与验收结果见 [管理端验收报告](admin-verification.md)。

## 构建和启动

需要 Go 1.26、Node.js/npm，以及 Linux Docker Compose（Windows 可使用现有 WSL Docker）。

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-admin.ps1
# 可选：证书加入局域网 IP 或域名；生产应使用组织签发证书
$env:ADMIN_TLS_HOSTS = '192.168.1.20,gateway.example.internal'
.\.build\gateway-admin.exe init-secrets
wsl -d Ubuntu-L4 -u root -- docker compose -f docker-compose.yml -f docker-compose.admin.yml up -d --build --wait
```

Linux：

```bash
bash scripts/build-admin.sh
ADMIN_TLS_HOSTS=192.168.1.20 .build/gateway-admin init-secrets
docker compose -f docker-compose.yml -f docker-compose.admin.yml up -d --build --wait
```

沿用原 `.env` 和 `config/gateway.example.yaml`；如果尚未配置，先按 README 初始化。首次启动导入 YAML 与它引用的环境凭证，后续以管理数据库中的版本为准。管理模式的 config 服务在运行快照已存在时不覆盖它，普通文件部署方式仍独立可用。

访问 `https://localhost:8443` 或 `https://<服务器地址>:8443`，账号默认为 `admin`，初始密码默认为 `12345678`。密码位于本机 `.admin/secrets/ADMIN_PASSWORD`，初始化工具不会输出凭证；管理员在本机读取并保存。初始化重复执行不覆盖现有 Secret 或证书。开发自签证书需在浏览器/客户端受信任；正式部署将证书和私钥替换为 `.admin/certs/tls.crt`、`tls.key`，证书需包含实际访问域名/IP。

仅管理入口映射到局域网；LLM 服务仍默认映射 `127.0.0.1:8080`。如需远程 Agent 接入，单独调整网关暴露地址及对应 TLS 代理。8443 不承担 LLM 请求代理。

在原生 Linux 主机上，Compose 将管理端映射到全部主机网卡。Windows 的 WSL2 NAT 环境可通过 localhost 访问；其他局域网设备访问 Windows IP 时，还需按主机网络配置设置 WSL 端口转发与防火墙，或使用支持局域网访问的镜像网络。此项目不自动修改 Windows 主机网络策略。

## 配置流程

1. **后端密钥**：新增名称、服务根地址和 API Key。地址不要重复包含 `/v1`，页面显示最终请求路径。留空密钥表示保留原值；禁用的后端不参与候选，模型必须至少有一个启用候选才能发布。
2. **模型服务**：建立逻辑名称、真实模型、上下文与输出限制及能力，最多两个候选；不配置价格或金额预算。
3. **用户管理**：新增用户、配置日/月 Token 统计额度，签发 Gateway Key；新 Key 发布后生效；管理员可在 Gateway Keys 列表点击“查看”重新读取和复制，60 秒后自动隐藏，查看操作记录审计。工具执行上报权限 `audit:write` 要求绑定 Agent ID。
4. **配置发布**：保存公开地址或额度切换开关；校验草稿，检查脱敏预览，再发布。界面中的配置表单均为草稿，运行版本以概览和版本列表为准。

模型入口示例：

```text
POST http://localhost:8080/models/coding/v1/chat/completions
Authorization: Bearer <Gateway Key>
{"messages":[{"role":"user","content":"你好"}]}
```

SDK Base URL 为 `http://localhost:8080/models/coding/v1`，SDK 的 model 填 `coding`。独立路径可省略 body.model；提供时必须与路径一致。统一入口 `/v1/chat/completions` 保持原有行为。

禁用用户会使其全部 Key 在发布后不可调用。撤销 Gateway Key、禁用后端以及额度切换开关同样采用发布机制。Token 配额只统计，不执行硬拦截；系统没有金额预算、价格或费用账本。正常发布不清零 Token 账本。

## 发布、回滚与恢复

Go 调用 Python 校验器生成不可变快照；Audit Store 持久保存新旧凭证脱敏集合后，内部控制代理原子替换快照，检查 Nginx 配置并平滑重载。确认实际 worker 版本与 Redis/Audit 健康后才标为 active。管理服务不挂载 Docker Socket。

失败时恢复原运行文件及 worker。版本状态包括 `publishing`、`active`、`superseded`、`failed`、`recovery_required`。出现 `recovery_required` 时先恢复控制代理及相关依赖，再重启 admin 服务触发状态核对；尚未恢复期间拒绝继续发布，避免覆盖不确定状态。

回滚创建新的发布版本，不删除旧版本、用量或审计。回滚包含用户与 Key 状态，可能重新启用之前禁用的凭证；页面要求先查看预览。回滚不自动覆盖当前编辑草稿，草稿仍可继续编辑后重新发布。

Nginx graceful reload 保留旧请求使用原配置。原有异步结算队列的 worker 意外退出风险仍存在，未知用量继续按原方案保留并离线核对，不用管理审计记录替代账本。

## 数据与权限

- `admin-data`：SQLite 配置、版本、密码哈希、服务端会话、AES-GCM 加密凭证和操作审计。
- `runtime`：网关私密明文快照及历史版本，只有网关、管理端和审计服务可挂载；禁止作为公开静态目录。
- `admin-logs`：运行事件 JSONL，最多 9 个约 8 MiB 文件，日志页支持时间、用户、模型、请求编号和事件筛选。
- `redis-data`、`audit-data`：沿用原账本和审计持久卷；Audit Store 增加管理员查询与读取记录，不改变用户查询权限。脱敏集合只持久化私密快照的版本引用，不在审计数据库复制密钥明文；退休版本至少保留至内容保留期结束。
- `.admin/secrets/ADMIN_MASTER_KEY`：32 字节 Base64 加密主密钥，必须与管理数据库一起备份。更换它不会自动重新加密历史数据。
- `.admin/secrets/ADMIN_INTERNAL_TOKEN`：独立控制面凭证，不能用于普通用户接口；控制接口没有宿主机端口映射。

管理操作和内容读取留痕。正文按需读取并以文本渲染，不执行模型输出的 HTML。截断、缺失、过期、Agent 上报与网关观察分别显示。Redis 或 Audit Store 不可用返回明确错误，不展示为零用量或空成功结果。

备份应包含管理数据库、加密主密钥、runtime、Redis 和审计卷。修改 TLS 材料或内部服务令牌后重新创建相关容器。`ADMIN_PASSWORD` 与 `ADMIN_USERNAME` 仅初始化管理员，修改文件或环境变量不会重置已存密码哈希；首版不提供密码重置页面。主密钥不匹配时管理端拒绝启动。不要使用 `down -v` 进行升级。退出管理模式前应备份当前快照并将所需配置迁移回 YAML/环境变量，不能直接用旧示例 YAML 覆盖管理端已发布配置。

## 管理 API

接口前缀 `/admin/api/v1`；登录使用 Cookie，会话有效 8 小时；写接口要求 `/session` 返回的 `csrf` 放入 `X-CSRF-Token`。

| 接口 | 用途 |
|---|---|
| `POST /login`、`GET /session`、`POST /logout` | 管理会话 |
| `GET /draft` | 获取配置草稿及 revision，不含密钥值 |
| `PUT /providers/{id}` | 后端配置，可选 secret 更新密钥 |
| `PUT /models/{id}`、`PUT /users/{id}` | 模型、用户草稿 |
| `POST /keys`、`DELETE /keys/{key_ref}` | 签发、撤销 Gateway Key |
| `PUT /settings` | 公开地址、额度切换开关 |
| `POST /validate`、`GET /preview` | 校验与发布预览，preview 可指定 version |
| `POST /publish`、`GET /versions` | 发布、版本分页；publish 可指定 version 回滚 |
| `GET /overview`、`GET /usage` | 实际运行状态、按用户和模型的 Token 用量 |
| `GET /logs`、`GET /operations` | 运行日志、管理员操作审计 |
| `GET /audit/requests`、`GET /audit/requests/{id}`、`GET /audit/traces/{id}` | 跨用户审计 |

草稿写操作携带 `{revision,value}`；后端密钥另带 `secret`。发布携带 `{revision,version?}`。并发草稿冲突返回 409。分页默认 50、上限 100；审计详情沿用游标，其他查询使用 offset。用量参数支持 user_id、model、day（YYYY-MM-DD）、month（YYYY-MM），时区固定 Asia/Shanghai。

## 验证

```powershell
cd admin
go test ./...
cd web
npm test
npm run test:e2e
```

浏览器验收默认使用隔离项目 `ai-gateway-admin-test`，管理端口 18443、网关端口 18081。`python -m tests.admin_acceptance` 会停止依赖、注入 Nginx 错误并重启该测试项目，严格只针对该命名项目。它不应指向生产服务。原网关回归仍使用 `bash scripts/test.sh`。

管理员通过 `POST /admin/api/v1/keys/{key_ref}/reveal` 查看 Gateway Key；接口要求管理员会话和 CSRF，禁止缓存，审计失败不返回明文。已撤销 Key 也可查看，但不会重新启用。后端 API Key 不支持查看。
