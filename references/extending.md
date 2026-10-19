# 按需接入新云与服务

## 先确定扩展哪一层

同一云中新账号/区域/Topic/Workspace：新增连接配置。相同数据源但日志格式变化：新增或调整 service profile。确实是新日志 API：新增 provider 模块。

“按需自动对接”是 Agent 根据具体需求查阅官方 API/SDK、编写适配并配置连接的流程，不意味着任意云在安装时已经支持。未知接口和未经验证的接入状态要明确报告。

## Provider 契约

在 `scripts/providers/PROVIDER_NAME.py` 中实现两个函数，配置中的 provider 使用模块名（小写字母、数字、下划线）：

```python
def configuration_status(connection):
    # 只返回变量是否存在、依赖状态；不联网，不返回凭据值。
    return {"environment": {"ENV_NAME": True}, "dependency": "sdk-name"}

def query(connection, start, end, limit, term=None, context=False):
    # start/end 是 UTC aware datetime，范围是 [start, end)。
    # term 是候选检索词；context=True 用于完整链路，不套错误筛选。
    return {
        "rows": [],
        "complete": True,
        "warnings": [],
        "resource": "日志空间或主题标识",
        "query_kind": "raw",
        "scanned": 0
    }
```

框架随后统一解析、按级别筛选、保留来源并组织结果。新的 SDK 只在该适配器中加载，避免某个云缺依赖导致其他云无法工作。

要支持自动任务中的 `trace --resource`，还须实现 `resource_id(connection)`，返回该实际查询任务的唯一资源标识，与 query 返回的 resource 一致。多资源适配器实现 `expand_connections(connection)`，先拆成各资源任务，再由 resource_id 精确筛选。该函数只读取配置/环境，不联网。未实现时限定资源的追踪明确失败，禁止退回全资源搜索。`context=True` 必须移除异常级别筛选和自定义异常 query_file，保留用户的服务范围条件。

## 接入流程

1. 确认云平台、日志产品、区域、身份方式和目标资源；读取目标平台官方接口文档。
2. 明确认证、查询语法、时间单位、分页/游标、限流、部分结果与错误语义。不要照搬 Azure 或 TLS 的返回结构。
3. 凭据由每条连接引用环境变量；非敏感资源参数进配置。新增依赖先创建/使用本 skill 的专属虚拟环境，再更新需求文件。
4. 实现适配函数，使用带时区时间和稳定原始记录信息，不丢弃来源。异常信息不得回显云端认证响应。
5. 观察真实日志样本，添加字段映射或解析规则。普通 INFO/DEBUG 排除仅在已确认的级别字段上生效，不新增状态码或路径规则。
6. 需要验证时，根据用户授权完成静态检查、样本检查或真实只读查询，报告实际覆盖；未经实际验证的 API 接入保持“待验证”。验证日志服务不等于授权发送 IM 消息。
7. 更新相应 reference、配置样例和 skill 路由。保持通用格式，不生成客户端专属 agents 配置，也不要求某个客户端的工具名称。

## 服务 profile

profile 的 `fields` 将标准字段映射到点分隔 JSON 路径，支持路径数组、嵌套 JSON 字符串及常见 Header 数组。`patterns` 的正则需要捕获组，优先命名为 `value`；`stage_patterns` 映射 received/completed/upstream 等阶段。包含用户输入的日志应限定 logger 和消息前缀，避免从请求正文提取生命周期字段。

通用 parser 的可选开关：

- `request_id_prefix: true`：识别消息开头的 Request ID，优先于 payload 标识。
- `prefer_level_prefix: true`：以 Loguru 级别前缀覆盖容器传输层级别。
- `bracket_fields: false`：关闭全文 `key=[value]` 扫描，防止结构化 payload 中的文本被提升为元数据。默认开启以兼容已有网关规则。
- `json_payloads`：规则数组，每项包含精确 logger、非空 prefix，可选 target。仅匹配该 logger 且消息以 prefix 开头时解码其后的 JSON/Python 字典；有 target 时存入对应命名空间，否则合并顶层。例如 `{"logger":"service.assets:_log_response","prefix":"upstream failed: ","target":"upstream_result"}`，再通过 `upstream_result.response.status_code` 映射字段。
- `operation`：可选标准字段，有值时消息增加操作类型。无模型语义的服务可通过 `not_applicable` 标记相关字段。

完整样例见 [plugin-api 服务规则](plugin-api.md) 和 `assets/profiles/plugin-api.json`。为可信字段选择映射时保留入站请求与上游调用的区别，尤其不能把上游 RequestId、URL、Authorization 当成本地请求身份和授权。

## 新通知渠道

IM 配置与云连接分离。首版 `cloud_log_delivery.py` 只实现飞书；新增渠道需要独立实现认证、大小限制和成功确认，再接入 `send_document` 的渠道选择。始终接收统一脱敏后的消息，确认成功后更新 outbox；不把网络发出等同于平台已接收。
