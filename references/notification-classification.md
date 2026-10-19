# Agent 错误分类协议

语义分类由宿主 Agent 完成；Python 不运行模型，不凭 HTTP 状态推断原因。先完成 investigation 要求的追踪，再按每个真实 event_id 写 analysis-file。没有具体错误或证据不足时省略 classification，保留 suspected/unknown 分析。

```json
{
  "真实事件指纹": {
    "certainty": "confirmed",
    "judgment": "上游明确拒绝输入内容",
    "recommendation": "核对输入内容和上游审核要求",
    "classification": {
      "category": "content_policy",
      "reason": "input_content_rejected",
      "subject": "input",
      "summary": "输入内容未通过审核",
      "evidence_event_ids": ["真实事件指纹"],
      "evidence_quote": "从实际错误字段复制的原文片段"
    }
  }
}
```

## 字段与证据

- `category` 必须取下表值。`reason` 为 3–64 位小写字母、数字和下划线，以字母开头；同一具体原因跨轮次使用相同值，不嵌入请求 ID、时间或 Key。
- `subject` 是实际受影响的参数、资源种类或处理阶段，1–128 字符。参数类错误填写精确参数路径，例如 `content[2].image_url`；不能把不同参数都写成 request。确实无更细对象时填 `request`，并使用具体 reason。
- `summary` 为 1–240 字符的简短事实摘要；不得用普通完成日志或状态码编造摘要。
- `evidence_event_ids` 引用当前事件，或该事件关联结果 `field_sources` 选中的错误证据，最多 20 个。`evidence_quote` 必须是其中 error_code/error_type/error_message 内的连续原文，长度 4–2000 字符。不要提前改写或脱敏证据片段；交给 Skill 统一处理。
- 只有 `certainty=confirmed`、证据引用匹配且关联无歧义/错误冲突的提议才参与语义分组。代码校验结构和证据归属，语义是否合理由 Agent 负责。

| category | 含义 | 建议 reason 示例 |
| --- | --- | --- |
| content_policy | 内容策略拒绝 | input_content_rejected / output_content_rejected |
| invalid_request | 请求参数无效 | missing_parameter / invalid_parameter_value |
| authentication | 身份验证失败 | invalid_credential / expired_credential |
| permission | 已识别身份无权限 | resource_access_denied |
| rate_limit | 明确的请求速率限制 | request_rate_exceeded / token_rate_exceeded |
| quota | 额度耗尽 | account_quota_exhausted |
| not_found | 模型或资源不存在 | model_not_found / resource_not_found |
| timeout | 明确超时 | upstream_connect_timeout / upstream_read_timeout |
| unavailable | 上游不可用 | upstream_service_unavailable |
| internal | 已有具体证据的内部错误 | response_decode_failed |

同一 category 不代表同一个错误。区分输入/输出、缺参/参数值错误、限速/额度、连接/读取超时；不要只填笼统的 upstream_error。新增具体 reason 应在业务约定中保持稳定。不能仅凭 400、429、500 或 phase skipped 进行分类。

## 合并与输出

Skill 以 category、reason、subject、原始错误码/类型，以及服务、环境、模型、授权身份、上游、方法/路径/操作、状态和已知结果等条件合并。分析和建议的自然语言措辞不参与分组；生命周期的不同日志可以合并展示，但仍独立保留事件和回执。

未提供分类或校验未通过时，仅对错误码、类型和错误原文一致的事件做确定性匹配；不自动删改正文中的参数或标识来猜测语义。无模型、无可信 Key 身份、无具体错误或证据冲突时保留独立分组。`send.classification_issues` 和详情报告会列出未采用分类的原因。

授权按状态目录内 HMAC 指纹比较，不能使用脱敏后的前后几位判断同一个 Key。Agent 输出的摘要、建议、分类字段和证据都由 Python 脱敏；Agent 不负责最终通知的脱敏。使用过额外 query/trace 原文时传入 `--secrets-input`，识别被分析再次引用的完整凭据。
