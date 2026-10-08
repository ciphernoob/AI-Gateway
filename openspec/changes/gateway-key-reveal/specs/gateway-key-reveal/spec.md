## ADDED Requirements

### Requirement: 管理员按需查看网关密钥
系统 SHALL 通过经管理员会话和 CSRF 保护的 POST 接口返回指定 Gateway Key，并禁止读取其他凭证引用。

#### Scenario: 查看已签发或已撤销密钥
- **WHEN** 管理员提交有效会话和 CSRF 并指定草稿中的 Gateway Key 引用
- **THEN** 返回原密钥，响应禁止缓存，记录不含明文的操作审计，保持其启用状态

#### Scenario: 无权访问
- **WHEN** 请求未登录、会话失效或缺少 CSRF
- **THEN** 拒绝返回密钥

#### Scenario: 非网关凭证
- **WHEN** 请求引用后端 API Key 或不存在的 Key
- **THEN** 返回 404 且不解密该凭证

#### Scenario: 审计失败
- **WHEN** 操作审计无法保存
- **THEN** 返回错误且不返回明文

### Requirement: 管理界面明文生命周期
系统 SHALL 默认隐藏密钥，仅点击查看时在弹窗显示并支持复制，不将明文写入列表、差异或浏览器持久存储。

#### Scenario: 查看并关闭
- **WHEN** 管理员点击查看后关闭弹窗、离开页面或等待 60 秒
- **THEN** 页面清理明文，迟到请求不得重新显示
