# TaaS 安全改造说明

## 平台管理员会话

- `POST /api/auth/login` 登录成功后签发数据库会话令牌。
- 会话有效期为 12 小时，服务端只保存令牌的 SHA-256 摘要。
- `POST /api/auth/logout` 撤销当前会话。
- `GET /api/auth/me` 校验会话并返回当前用户信息。
- TaaS 新控制面和旧版兼容管理接口均要求有效的“管理员”会话。
- 客户门户继续使用独立的 TaaS 用户 API Key，不与平台管理员会话混用。

## 密码和开发模式

- 旧平台明文密码首次成功登录后自动升级为 `scrypt` 哈希。
- 新密码不会以明文保存。
- `dev-admin-token` 默认关闭，仅在本地临时调试时显式设置 `ALLOW_DEV_ADMIN_TOKEN=true`。
- 生产环境必须保持 `ALLOW_DEV_ADMIN_TOKEN=false`，并使用 HTTPS 保护登录和 API 请求。

## 启动配置

```powershell
$env:ALLOW_DEV_ADMIN_TOKEN = "false"
$env:DB_POOL_MIN = "2"
$env:DB_POOL_MAX = "20"
```

数据库启动时会自动创建 `platform_sessions` 表和活动会话索引。
