## Context
Gateway Key 已使用 AES-GCM 存在 SQLite，草稿只公开凭证引用。

## Goals / Non-Goals
提供管理员按需查看，保留默认脱敏及可追溯性。不提供后端 Key 查看，不修改 Key 启用状态。

## Decisions
使用 POST /admin/api/v1/keys/{key_ref}/reveal，复用会话与 CSRF。只允许草稿 api_keys 中的引用；已撤销 Key 也可查看，但不会因此重新启用。解密后先写操作审计，再返回明文；审计失败时拒绝返回。响应 Cache-Control: no-store。
列表不包含明文。点击查看弹窗显示，支持复制；关闭、切换页面、组件卸载时清理内存，并在 60 秒后自动隐藏。请求期间离开页面或过期会丢弃迟到结果。浏览器持久存储不保存明文。

## Risks / Trade-offs
管理员能够读出已保存凭证，这是用户明确要求的权限变化。通过单凭证解密和引用白名单防止接口扩展为任意后端密钥读取。

## Migration Plan
无需迁移数据。更新管理程序即可使用，配置无需重新发布。
