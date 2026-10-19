# 统一消息与 IM 配置

## 飞书自定义机器人

首版实现飞书 Webhook 主动通知，无需用户运行特定 Agent 客户端，也不需要飞书长连接机器人或开放平台应用。

在主配置的 `notifications` 中定义渠道：

```json
{
  "feishu-alerts": {
    "provider": "feishu",
    "webhook_env": "FEISHU_WEBHOOK",
    "secret_env": "FEISHU_SECRET",
    "keyword_env": "FEISHU_KEYWORD",
    "timeout_seconds": 30
  }
}
```

`FEISHU_WEBHOOK` 支持单个 HTTPS 地址，以及现有的机器人 JSON 数组格式：

```dotenv
FEISHU_WEBHOOK=[{"webhook":"https://open.feishu.cn/open-apis/bot/v2/hook/BOT_A","secret":"BOT_A_SECRET"},{"webhook":"https://open.feishu.cn/open-apis/bot/v2/hook/BOT_B","secret":"BOT_B_SECRET"}]
```

配置几个机器人就向几个目标推送。每项 secret 对应各自的签名密钥；省略时使用 `FEISHU_SECRET`，显式空字符串表示不签名。若启用关键词校验，配置 `FEISHU_KEYWORD`，关键词并入每张卡片的总标题，不在正文顶部单独显示提示行。机器人有 IP 白名单时，实际执行环境的出口 IP 需要符合其配置。空列表、无效地址和重复 Webhook 会在 doctor 中报错。

多个机器人可放在同一渠道数组内，共用巡检任务的状态目录；需要不同路由时再定义多个通知渠道，使用不同变量名和状态目录。`send --channel CHANNEL_ID` 向该渠道的全部机器人发送。创建、修改配置不代表立即发送消息。

## 按相同错误合并展示

通知渲染会把同时满足以下条件的事件合并为一条展示：同一云提供方、服务和环境；同一模型与授权 Key；错误代码、类型和消息相同；错误阶段、最终结果及响应状态相同。没有模型、授权 Key 或可识别错误的事件不会参与合并。事件跨不同 Topic/资源时可合并，合并项会列出涉及的来源。

合并摘要显示事件数、时间范围和来源。请求标识、事件 ID 不会冒用某条代表记录：消息说明底层事件及回执仍逐条保留，追踪完整性和最终结果不从分组推断。队列仅保存状态目录专用密钥计算的授权 HMAC 指纹，不保存该授权 Key 的明文；状态目录应按现有日志数据权限妥善保护。旧队列记录如果在升级前已脱敏且没有指纹，将继续逐条展示，因为无法安全恢复原 Key 是否相同。

直接渲染含原始授权信息的 query/normalize 文件可在内存中分组；渲染 `scan` 或 `claim` 输出时需指定该批次使用的状态目录，供工具读取 HMAC 指纹：

```text
PYTHON scripts/cloud_logs.py render --input runtime/scan.json --state-dir ABSOLUTE_STATE_DIR --output runtime/notification.txt
```

飞书默认发送 `msg_type=interactive` 卡片。同一批次中，各合并组和未分组事件汇总展示，组内列出涉及来源；不按云、服务或 Topic 将整批消息拆开发送。每个告警组标题独立加粗，组与组之间使用分隔线；内部小节使用加粗标题和留白，不重复插入分隔线。卡片分区展示请求信息、具体错误、简短分析和建议；正文沿用 `assets/notification.txt`，`render` 仍可生成本地文本预览。动态标题转义后加粗，日志正文和分析放入纯文本组件，授权统一脱敏。仅超过卡片字节数或组件容量时分段，保留中文字符，不静默截断；每张都带机器人关键词。

各机器人独立确认成功，一个目标失败会继续尝试其他目标；整个批次的所有目标完成才确认事件。队列持久化每个事件的目标回执，重试跳过已完成目标。未完成目标如果有部分卡片已接收，重试仍可能重复该目标的片段。批量卡片之间留有发送间隔；多个并行任务共用机器人时，仍需统筹平台限流。其他 IM provider 需先实现渠道适配。

## 固定字段

消息包含：发生时间、日志来源、级别、事件标识、请求/追踪标识、脱敏授权、请求路径、模型、上游、服务/上游状态、错误来源、具体错误、错误分析和建议。有 operation 时增加“操作类型”，例如 CreateAsset；网关 profile 将服务状态标为“网关”。

卡片不展示“请求人”和“关联与追踪”整段（追踪状态、关联方式、关联依据、追踪完整性、请求结果）。这些字段仍保留在 JSON 中，供 Agent 分析及发送前检查使用。

- `event_id` 是本工具生成的日志指纹；`request_id` / `trace_id` 来自原始元数据，两者不能混用。
- 级别非 INFO/DEBUG 并不等于请求最终失败。WARN/NOTICE 等记录可以发送，最终请求结果由追踪证据决定。
- 没有某字段写“未记录”；服务 profile 中明确 `not_applicable` 的字段写“不适用”。
- 原始错误与分析结论分别展示。推测不能写成已确认根因，时间接近不能写成标识匹配。
- 具体错误只使用 error_code/error_type/error_message，不用普通 message 或整段 REQUEST_COMPLETED 文本回退。缺少错误内容写“未记录”；未知错误不因为普通日志相同而合并。
- 补全错误展示关联事件来源，原记录自带错误展示“当前记录”。每个事件的追踪状态和证据在 JSON 中单独保留。
- 不添加之前取消的路径过滤，也不添加“监控条件”说明行。

## 发送前的追踪要求

Agent 必须按 [执行流程](agent-workflow.md) 完成 `investigation.next_actions` 并 enrich。`send` 在调用网络和变更发送状态之前重新检查待执行任务；不能仅修改根级 status 跳过检查。

`render` 可在本地预览尚未追踪的事件；是否完成追踪须检查 JSON 中的 investigation。已尝试但失败、为空、被截断或定位受阻的事件可发送注明限制的报告；没有具体错误证据时不能编造根因。使用宿主其他发送工具时沿用同一要求，预览不得 ack。

## 分析输入

Agent 可给 `render` / `send` 提供 `--analysis-file`，内容以真实 event_id 为键：

```json
{
  "实际事件指纹": {
    "certainty": "suspected",
    "judgment": "根据上游错误判断参数可能无效，尚需核对对应资源",
    "evidence": "事件指纹及错误字段，说明结论依据",
    "recommendation": "核对请求参数与上游资源状态"
  }
}
```

certainty 使用 `confirmed`、`suspected`、`unknown`。脚本不调用 LLM，也不凭状态码生成确定根因；由宿主 Agent 阅读日志并填写分析。未提供分析时显示“暂无法确认原因”。

卡片的“错误分析”只展示简短原因和处理建议，通常各一句。不要在这里加入联调过程、日志计数或长事件指纹；详细 `evidence` 留在本地分析文件中。推测仍标注“可能”，未确认的结论标注“待确认”。

## 授权信息处理

查询阶段允许 Agent 得到完整授权值。云访问凭据通过环境变量配置，请求 Header 内的授权值可用于精确检索。

发布时统一脱敏，包括授权字段、错误原文、日志片段和分析引用。默认保留 Key 前 4 位和后 4 位，中间为八个星号；长度不超过 12 的值全部隐藏。`Bearer` / `Basic` 等认证类型保留。支持结构化和常见文本形式的 Authorization、X-API-Key、auth_key、api_key、license_key 等字段。

`query` 输出是内部分析材料，可能含原文；`render` 输出才是可发送文本。`scan` 队列保存脱敏诊断字段和授权 HMAC 分组指纹，不保存原始请求/响应或授权 Key 明文；补查完整授权时使用 query/trace。分析使用了额外原始日志时，把该 JSON 传给 `render` 或 `send` 的 `--secrets-input`，用于识别分析中可能再次出现的完整凭据。它不会被直接附加到消息。

不因安装 skill 自动发测试消息。实际调用 send 的目标、内容和任务范围需要在用户的已有发送授权内。官方自定义机器人文档入口：https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
