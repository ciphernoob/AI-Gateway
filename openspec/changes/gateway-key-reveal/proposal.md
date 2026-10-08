## Why
管理员需要重新查看已签发的 Gateway Key，以完成客户端配置，避免因忘记保存而反复签发。

## What Changes
- 允许管理员在用户管理页按需查看和复制 Gateway Key。
- 通过独立 POST 接口鉴权、校验 CSRF、解密单个凭证并记录不含明文的操作审计。
- 替代 Gateway Key 只能展示一次的原规则；后端模型 API Key 仍不回显。

## Capabilities
### New Capabilities
- `gateway-key-reveal`: 已签发网关凭证的管理员查看。
### Modified Capabilities

## Impact
Go 管理 API、Vue 用户管理、管理文档和测试；无需改变网关数据面或凭证存储格式。
