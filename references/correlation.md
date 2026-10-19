# 服务格式与关联规则

## 字段归一化

云平台解决“怎样取日志”，服务 profile 解决“日志字段是什么意思”。profile JSON 中可设置：

- `parser`：generic 或 gateway。
- `timezone`：日志时间没有时区时采用的显式偏移；查询输入自身必须有时区。
- `timestamp_unit`：数字时间戳的 s/ms/us/ns；TLS 原始 `__time__` 固定毫秒。
- `fields`：标准字段到来源路径的映射，按顺序取首个非空值。支持点路径，字段名大小写不敏感，先查正文顶层元数据，再查云返回行。
- `patterns`：标准字段到正则的映射；字段映射无值时使用。第一个捕获组或名为 value 的捕获组是字段值。正则来自用户/维护者的 profile，不执行日志里的正则或代码。
- `level_map`：服务自身的数值/别名级别映射，例如 AppTraces 的 0/1/2/3/4 对应 DEBUG/INFO/WARNING/ERROR/CRITICAL。未映射值仍保留。
- `stage_patterns`：服务阶段到正则的映射，可配置 received/completed/upstream 等。
- `heuristic_window_seconds`、`heuristic_fields`：上下文候选的时间范围和辅助字段。
- `not_applicable`：明确不适用的输出字段。

标准字段包括 timestamp、level、request_id、trace_id、correlation_id、business_id、actor、authorization、method、path、model、upstream_provider、upstream_model、upstream_channel、gateway_status、gateway_code、upstream_status、error_code、error_type、error_message、instance、container、logger、event_type。

例如服务把用户和请求 ID 放在 context 下：

```json
{
  "parser": "generic",
  "timezone": "+00:00",
  "fields": {
    "request_id": ["context.requestId"],
    "actor": ["context.userId"],
    "authorization": ["headers.Authorization"],
    "level": ["severityText", "level"]
  },
  "heuristic_window_seconds": 30,
  "heuristic_fields": ["path", "actor"]
}
```

不要递归把 request/response body 中任意 request_id 当成当前请求标识。自定义字段需确认其来源和语义。没有标识时保留空值；生成的日志指纹只用于事件定位和投递去重。

## 关联等级

| 条件 | 方式 | 结论边界 |
| --- | --- | --- |
| 有可信 request_id/trace_id/correlation_id | 标识匹配 | 仅关联该作用域内相同标识的日志 |
| 有任务/订单/消息标识 | 业务对象关联 | 可能涉及多次请求 |
| 无标识，有时间、实例和其他匹配字段 | 上下文推测 | 可能相关，不能认定同一次请求 |
| 信息不足 | 单事件上下文 | 无法关联 |

默认同一连接且同一资源内匹配，包括同一连接配置多个 Topic 的情况。跨云、跨空间/Topic 匹配需确认这些服务确实传播同一标识，才用 `--cross-connections`；不会自动用同一 Key、用户、模型或相近时间跨云串链。相同 trace_id 内出现多个 request_id，应展示为多请求过程。

无标识候选必须与锚点同服务/环境、同实例、在时间范围内，并至少有一个配置的辅助字段相同。候选始终围绕锚点比较，不做容易把大量请求串在一起的传递合并。可以进一步观察进程、连接、任务等信息，但线程复用、连接复用本身不保证请求唯一。

## 网关兼容

预置规则沿用原项目的 JSON、日志前缀、平衡括号字段和结束记录 request 字典解析方式。保留旧 err_msg/error 优先，upstream_error 填补缺失的 code/type/message/status。识别 VIDEO_CREATE_TIMING 与 video_router 上游调用。

网关成功/失败以明确 completed 事件为依据；多入口、多结束或多个请求 ID 标注歧义。上游失败不直接等于最终失败；流处理异常单独标注。异步计费与请求响应分开观察。

`observed_boundaries` 仅表示已采集日志里观察到单一入口和结束，不能保证云采集之外没有缺失。未记录时间、查询截断、没有入口/结束、业务对象或推测关联都不宣称完整链路。

关联器不会发起无限追查。需扩展上下文时，根据现有证据执行新的明确时间范围查询，并保留来源与查询完整性。没有足够历史信息时如实报告；未来精确追踪需要应用传播标识。

`enrich` 用明确标识把补查结果中的缺失字段带回原告警批次，并记录字段来源。多入口、多结束等歧义时不自动补全；冲突字段留空，结束成功的记录不借用重试阶段的错误作为最终错误。无 ID 或仅业务 ID 的场景继续由 Agent 做有边界的上下文分析。

## 追踪覆盖与增量证据

`investigation` 的执行协议见 [Agent 执行流程](agent-workflow.md)。`trace_status` 表示是否实际查过相应范围及查询是否完整，`completeness` 表示是否观察到请求边界，`error_evidence_status` 表示是否有具体错误。三者独立；完整查询仍可能没有错误详情或请求边界。

`enrich` 核对来源、标识和查询时间范围，保留 `context_records`、`context_event_ids`、`error_evidence`、`field_sources`、`trace_queries`。空/失败的 trace 依靠 `matched_identifier` 和来源诊断记录尝试；带 `selection=exclude_INFO_DEBUG` 的上下文只能补充字段，不能解除全级别追踪任务。

多次 enrich 应以最近输出为输入。新错误与旧证据冲突时，撤回旧的自动补全并标记 `error_conflict`；原记录自带字段保留。错误代码、类型、消息须能由同一条证据一致支持，不从不同重试拼接。各条错误仍保留供 Agent 分析。成功结束记录可保留重试错误证据，但不将其填为最终错误。
