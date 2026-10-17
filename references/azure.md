# Azure Log Analytics

适配器：`scripts/providers/azure_log_analytics.py`。使用标准库调用 Azure 客户端凭据认证和 Log Analytics REST API，无需绑定某个 Agent 或 Azure CLI。

## 配置

| 连接配置字段 | 默认环境变量 / 值 |
| --- | --- |
| tenant_id_env | AZURE_LOG_TENANT_ID |
| client_id_env | AZURE_LOG_CLIENT_ID |
| client_secret_env | AZURE_LOG_CLIENT_SECRET |
| workspace_id_env | LOG_ANALYTICS_WORKSPACE_ID |
| table | ContainerLogV2 |
| time_column | TimeGenerated |
| content_column | LogMessage |
| container_column | ContainerName |

`containers` 可限制服务实例范围；空数组表示不添加容器条件。其他表通过上述字段和服务 profile 适配。例如 AppTraces 可配置其消息列和字段映射，不假设所有表都是网关日志。

`search_columns` 可指定检索词查询的候选列，默认包括 FullLog、RequestId、trace_id、TraceId、OperationId。字段通过 column_ifexists 读取，缺失列不会导致整条查询失败。自定义 context.requestId 等 JSON 路径时，确保其承载列在检索范围内；字段映射负责后续精确复核。

默认身份地址为 `https://login.microsoftonline.com`，查询地址为 `https://api.loganalytics.azure.com`，scope 为 `https://api.loganalytics.io/.default`。其他 Azure 云需按官方文档配置 `authority_host`、`api_endpoint`、`scope`；三者要匹配。身份需要目标 workspace 的日志查询权限。

## 查询行为

使用明确的 UTC 开始/结束时间，按事件时间升序取回原始列，补充 `FullLog`、`TimeGenerated`。级别在通用解析层复核，不包含任何网关状态码排除条件。检索词用于缩小候选范围，trace 随后以结构化标识精确复核。

默认读取 max_rows + 1 行，以识别截断。Azure 返回 PartialError 时保留成功数据并标记不完整。失败输出 HTTP 状态和安全诊断，不回显认证响应、Token 或请求载荷。

自定义 KQL 使用 `query_file`；聚合结果不能直接声称是原始请求链路。为完整 trace，适配器使用该连接的基础表、字段和容器配置，不复用可能只查错误的自定义语句。

接入新表前阅读其真实 schema，配置可信的字段映射；不要递归搜索任意请求正文中的 request_id。Azure 文档入口：

- https://learn.microsoft.com/azure/azure-monitor/logs/api/overview
- https://learn.microsoft.com/azure/azure-monitor/logs/api/access-api
