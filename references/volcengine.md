# 火山引擎 TLS

适配器：`scripts/providers/volcengine_tls.py`。Python SDK 是 `volcengine` 包；在本 skill 的独立虚拟环境安装 `scripts/requirements.txt`。

## 配置

| 连接字段 | 默认变量 / 值 |
| --- | --- |
| access_key_env | VOLC_ACCESS_KEY |
| secret_key_env | VOLC_SECRET_KEY |
| topic_id_env | VOLC_LOG_TOPIC_ID |
| session_token_env | VOLC_SESSION_TOKEN，STS 场景可选 |
| endpoint | tls-cn-shanghai.volces.com |
| region | cn-shanghai |

连接身份需要对应 Topic 的 SearchLogs 权限。同一账号、区域、服务规则下的多个 Topic 可共用一条连接；不同账号、区域或服务格式使用多条连接，可以复用或分别引用凭据环境变量。只查询明确配置的 Topic，不枚举账户其他日志资源。

### 多 Topic 配置

保留原来的 `topic_id_env` 单值写法；该变量也可填写 JSON 字符串数组。多 Topic 还可以在连接中直接设置非敏感资源数组：

```json
{
  "id": "volc-orders", "provider": "volcengine_tls", "service": "orders", "environment": "prod",
  "profile": "profiles/generic.json", "endpoint": "tls-cn-shanghai.volces.com", "region": "cn-shanghai",
  "access_key_env": "VOLC_ACCESS_KEY", "secret_key_env": "VOLC_SECRET_KEY",
  "topic_ids": ["TOPIC_A", "TOPIC_B"]
}
```

如果资源也由环境管理，用 `"topic_ids_env": "VOLC_LOG_TOPIC_IDS"` 替换 topic_ids，并设置 `VOLC_LOG_TOPIC_IDS=["TOPIC_A","TOPIC_B"]`。优先级为 topic_ids、topic_ids_env、topic_id_env；选择一种即可。

CLI 将 Topic 展开为独立资源任务，最多四个资源并发。各 Topic 独立分页、应用行数上限并报告失败；一个 Topic 失败保留其他 Topic 的结果。记录保留原连接 ID 和具体 resource（Topic ID），消息据此展示日志来源。默认请求关联也按资源隔离，避免不同 Topic 的同名 ID 被误合并。

网关与 plugin-api 格式不同，分别使用 `volc-cn` 和 `volc-plugin` 连接及各自 profile，凭据可共用；推送仍汇总到同一批卡片。配置细节见 [plugin-api 服务规则](plugin-api.md)。

## 原始日志查询

默认构造容器范围和可选检索词，按原始日志 API 分页。每页最多 100 条，使用 SDK 返回的 Context；到 ListOver 才确认原始查询完成。上限、重复/缺失游标以及后续页失败均标记不完整。

原始字段 `__time__` 按毫秒解析；容器与实例取 `__container_name__`、`__pod_uid__` / `__pod_name__`，正文通常在 `__content__`。不假设日志正文均为 JSON。

默认不添加状态码过滤。所有取回记录交给服务规则判断日志级别，排除 INFO、DEBUG；不要使用全文 NOT INFO/DEBUG，以免误排错误内容里出现这些词的记录。日志量较大时，可在已确认字段结构后配置自定义查询，同时检查结果是否完整。

## SQL 查询结果

指定 `query_file` 后可使用用户的 TLS SQL。正确处理两种返回：

- `analysis=False`：从 `logs` 读取，按 Context 继续分页。
- `analysis=True`：从 `analysis_result.analysis_data` 读取；不能继续用原始日志 Context 翻页，也不能误判为没有日志。

检查 `result_status` 是否 complete，检查 analysis_data 为对象数组。SQL 的 LIMIT 和聚合决定实际覆盖范围；服务器完成这条 SQL 不等于获得该时间段的全部原始日志。

TLS 查询语法按平台支持编写，不沿用其他云的 `!=` 语法。可使用已支持的 `NOT IN`、`NOT LIKE` 等表达式，但本 skill 没有默认状态码排除项。完整 trace 使用基础容器查询，避免复用只筛错误的 SQL。

官方日志服务文档入口：https://www.volcengine.com/docs/6470
