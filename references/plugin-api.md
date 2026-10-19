# plugin-api 服务规则

配置样例中的 `volc-plugin` 使用火山 TLS、`plugin-api-svc` 容器和 `assets/profiles/plugin-api.json`。它与网关连接共用火山凭据，Topic 使用 `topic_ids` 或 `VOLC_PLUGIN_LOG_TOPIC_IDS` 环境变量中的 JSON 字符串数组。样例默认关闭，配置实际 Topic 后启用。

## 查询与解析

巡检查询整个容器中非 INFO/DEBUG 的日志，不限制特定 logger、状态码或错误码。没有 request_id 的 HTTP 工具错误同样保留；旧程序中针对余额不足等错误的排除规则不适用于此 skill。

已适配的日志格式：

- Loguru 前缀中的真实日志级别，以及 `Request ID: ID - ...`、`Request ID [ID] ...` 两种请求标识。
- `service.seedance_asset_service:_log_response` 的 `seedance asset upstream response failed:`：解析操作、上游状态、ResponseMetadata.Error 或 error 中的错误详情；response.content 可为 JSON 字符串。
- `service.seedance_asset_service:_log_request` 的上游请求：只从已知位置提取操作，不把上游 URL 当成客户端请求路径。
- `api.middlewares.auth_middleware:dispatch` 的请求 URL 和 Header：补全入站路径、Authorization；支持 Python 字典形式。
- `sdks.anspire.jdcloud_asset_auth_verify:process` 的鉴权结果：从 data.customer_id 补全请求人。license_key 与 Header 授权统一脱敏。
- `utils.httpUtil:do_request` 的 `Request failed:`：提取 code/msg。没有目的地或请求标识时，不猜测上游身份或关联请求。

嵌套上游 RequestId 不会覆盖本地请求 ID。只有已确认的 logger 和消息前缀才解码相应 payload，并放入独立字段命名空间；正文中的 request_id、状态或阶段文本不能充当本地生命周期元数据。

其他 plugin 错误仍保留日志正文供 Agent 分析。该 profile 不保证所有插件业务均有完整字段；新增格式先验证样本再添加规则。缺失模型和上游字段在卡片中省略，内部按未知处理；不能仅根据某类素材操作推断整个 plugin 服务都不涉及模型。

## 请求追踪

按相同连接、Topic、本地 request_id 补查所有级别日志。鉴权开始标记入口；`api.routes.asset_library:_call_seedance_asset_action` 的 `seedance asset action end, action=..., http_status=...` 才标记最终响应。鉴权成功、鉴权结束或单条上游失败均不能代表最终请求结果。

上游失败后最终成功时报告最终成功，保留错误尝试；没有结束记录时结果未确认，重复入口/结束或多个请求 ID 时标记歧义。无 request_id 的记录只能作为独立事件或带“推测”标记的上下文候选。

使用 `enrich` 从已验证的上下文补全请求人、脱敏授权、入站路径和操作。错误分析保持原因、建议各一句。通知与其他服务汇总，每条记录保留 `volcengine_tls / volc-plugin / TOPIC_ID` 来源。
