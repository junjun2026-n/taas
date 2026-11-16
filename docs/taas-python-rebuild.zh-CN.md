# TaaS Python 重构说明

## 重构目标

TaaS 后端由单个大型接口文件逐步重构为 Python/FastAPI 领域模块。架构参考 new-api 的渠道、模型能力、令牌、统一网关、调度、计费和日志分层，但不复制其 Go 源码。

参考项目使用 AGPL-3.0。本项目当前实现为独立 Python 代码，若后续直接引入参考项目源码，需要单独评估并遵守其许可证要求。

## 新目录

```text
backend/app/taas/
├── api.py           # OpenAI 兼容和管理接口
├── gateway.py       # 上游转发、重试、统计
├── repositories.py  # 用户、渠道、模型、配置数据访问
├── routing.py       # 渠道评分和调度
├── schemas.py       # 请求数据模型
└── tables.py        # TaaS 新表和索引
```

原 `backend/app/api/taas.py` 暂时保留用户、充值、套餐及旧接口，保证现有数据和前端功能不中断。新开发应优先进入 `backend/app/taas`。

## 统一网关

OpenAI 兼容基础地址：

```text
http://服务器IP:8000/api/taas/v1
```

模型列表：

```text
GET /api/taas/v1/models
```

聊天补全：

```text
POST /api/taas/v1/chat/completions
```

请求认证：

```http
Authorization: Bearer taas_xxx
```

客户使用购买后生成的 TaaS API Key。网关根据请求中的 `model` 查询可用渠道，并按优先级、权重、品质、成功率、延迟、利润和渠道额度进行调度。

## 模型能力配置

后台 API Key 接入中的“模型范围”使用逗号分隔，例如：

```text
gpt-5,gpt-5.3,deepseek-chat
```

保存渠道后执行：

```text
POST /api/taas/channels/{channel_id}/models/sync
```

系统将模型范围同步到 `taas_channel_models`，支持为每个模型配置上游模型名、优先级、权重以及输入输出价格。

## 调度和重试

网关设置接口：

```text
GET /api/taas/gateway/settings
PUT /api/taas/gateway/settings
```

可配置：

- 上游失败重试次数
- 上游请求超时
- 上下文缓存开关
- 最大上下文消息数

当上游返回 5xx 或连接失败时，网关自动尝试下一个可用渠道。4xx 会直接返回，避免重复提交无效请求。

## 日志与统计

每次网关调用写入：

```text
taas_gateway_request_logs
```

记录请求 ID、客户、套餐、渠道、请求模型、上游模型、状态码、输入输出 TOKEN、延迟和错误信息。

查询接口：

```text
GET /api/taas/gateway/logs?limit=100
```

响应头包含：

```text
X-TaaS-Request-Id
X-TaaS-Channel
```

便于定位每次请求实际使用的渠道。

## 数据迁移

后端启动时自动执行：

1. 保留现有用户、套餐、充值、API Key 和使用记录。
2. 为现有启用渠道创建模型能力记录。
3. 创建网关设置、模型能力和网关请求日志表。
4. 前端配置脚本切换到统一 `/api/taas/v1` 地址。

## 启动与验证

```powershell
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

验证模型接口：

```powershell
$headers = @{ Authorization = "Bearer taas_xxx" }
Invoke-RestMethod -Headers $headers http://127.0.0.1:8000/api/taas/v1/models
```

验证聊天接口：

```powershell
$headers = @{ Authorization = "Bearer taas_xxx"; "Content-Type" = "application/json" }
$body = @{ model = "gpt-5"; messages = @(@{ role = "user"; content = "你好" }) } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Headers $headers -Body $body http://127.0.0.1:8000/api/taas/v1/chat/completions
```

## 当前边界

本阶段已经完成 Python 网关核心、模型能力、渠道调度、失败重试、上下文缓存、按 TOKEN 计费、请求日志、API Key 认证和前端统一地址迁移。原有用户注册、充值、支付、套餐和客户门户接口仍由兼容模块提供，数据保持不变。

后续可继续拆分旧文件中的用户认证、支付、套餐和客户门户接口，并增加流式 SSE、图片、音频、嵌入和厂商协议适配器。当前网关对普通 JSON 聊天补全可用，流式请求会等待上游响应完成后返回，不适合对实时首字延迟有严格要求的场景。
