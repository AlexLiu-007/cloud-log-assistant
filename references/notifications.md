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

## 分类与合并

语义分类由 Agent 根据追踪证据完成，协议见 [错误分类字段](notification-classification.md)。脚本验证证据归属、字段和分组范围，完成统计、脱敏和渲染。旧 analysis-file 仍可使用；没有有效分类时仅按具体错误字段一致匹配，不猜测同类。

一级按 Key 身份 + 模型，未检测到模型时按 Key 单独分组。二级按具体错误字段或通过证据验证的 category/reason/subject 与原始错误码/类型合并。来源、状态、上游、路径和日志阶段等差异不再拆组；跨来源的请求关联仍限定来源资源。Key 使用状态目录专用 HMAC 身份，缺少可信 Key 身份时不按掩码猜测合并。

`render` 预览 scan/claim 输出时传入同一 `--state-dir` 读取 Key 指纹。没有指纹时不能凭相同脱敏值合并。`enrich` 补全后由 Agent 重写分析；不要手动将补全后的明文 Key 拼入通知。

## 精简卡片与详情

卡片的一级标题只显示脱敏 Key 与已记录的模型，概要保留日志数、错误组数、时间和来源。每个错误显示一次错误信息、一份共同分析/建议，下面集中列出 request_id，不再按每条日志重复展示。request_id 精确去重、保持大小写，不截短；ID 值数量不等同于跨来源的唯一请求数。缺少 ID 的日志注明条数，不能用 trace_id/correlation_id 代替。

卡片保留所有错误组，优先显示严重级别高的组。缺少具体错误仍明确写“错误信息：未记录”。状态、路径、上游、事件 ID、错误来源及完整分析在详情中查看。禁止展示“请求人”和“关联与追踪”整段（追踪状态、关联方式、关联依据、追踪完整性、请求结果）。查询失败/不完整等必要限制仍单独说明。

带持久状态目录的内置 send 会先保存脱敏详情到 `STATE_DIR/reports/编号.json`，再生成卡片。长原文显示至多 240 字符，多种原文显示明确标注的代表样本；分析/建议至多 160 字符，其余内容保留在详情。代码先脱敏再截取。卡片给出详情编号，可要求 Agent 查询：

```text
PYTHON scripts/cloud_logs.py report --state-dir ABSOLUTE_STATE_DIR --report-id CARD_REPORT_ID --output runtime/detail.json
```

详情包含该批次全部诊断字段、各事件分析和分类校验结果，排除 raw，统一脱敏。它是宿主本地报告，不假设飞书能访问本地文件，也不生成虚假的网页按钮。没有持久报告时 render/单次 send 保留完整错误内容；仅按卡片容量分段，不静默截断。

飞书使用 interactive 卡片。同一目标的各组汇总发送，仅超过容量时拆分；正文纯文本，标题转义，保留机器人关键词。所有原始事件、事件 ID 与逐目标回执独立保存。

## 跨轮次重复提醒

通知渠道可设置：

```json
{
  "repeat_interval_seconds": 900,
  "repeat_escalation_count": 100
}
```

默认首次出现的错误立即发送；同组新日志在上次确认发送后的 900 秒内延后汇总。达到截止时间后的下一次 scan/claim 才领取待发事件，脚本不常驻、不新增调度。`repeat_interval_seconds=0` 关闭延后；可配置范围 0–86400 秒。

以下情况提前提醒：新模型/Key/具体错误形成新组，日志严重级别上升，CRITICAL/FATAL，或同组同目标累计至少 100 条待发日志。`repeat_escalation_count` 控制待发日志条数阈值，0 关闭，最大 1000000；该阈值不是失败率，也不是失败请求数。查询失败/不完整、追踪受阻/失败/部分完成、分类校验失败、同组分析不一致/覆盖不足、无可靠分组依据时不执行重复延后。不从安静窗口推断恢复。

此策略只用于有持久 outbox 的内置 send；单次 query/trace 直接发送不执行跨轮次延后。已延后的事件及脱敏上下文仍在队列，**不会提前记作已发送**。到期 claim 自动带回关联信息和 notification_analyses，可在证据未改变时复用；再次 enrich 会清除旧分析，由 Agent 重评。

send 返回 acknowledged_events、deferred_events、next_notification_at（Unix 秒）和各目标状态。scan/claim 返回 deferred_count；available_count=0 时结束本轮，等待已有调度的下一次运行，不能因 pending_count 非零立即反复 claim。没有新日志时也需正常 scan/claim，才能送出到期汇总。

## 分段确认与重试

各机器人独立确认成功。投递前冻结该目标的脱敏卡片内容；每段成功后持久化进度，全部分段完成后才记录该目标的事件回执。失败目标重新 claim 后继续原内容的下一段，已完成目标跳过。多目标均成功的事件才最终确认，其余事件保持待发/延后。

恢复未完成计划时，claim 会连同该计划涉及的事件一起领取，可能超过新的 batch-size；维持原连接范围和同一状态目录。不得手动删掉批次中的事件。网络超时或成功后本地写入失败仍可能留下结果不确定的片段，重试可能重复；不宣称端到端恰好一次。

状态目录保存通知上下文、分段进度和脱敏详情，按日志数据权限保护。purge 清理过期已确认事件及已完成计划，不删除待发事件；报告文件按宿主的日志保留策略管理。

## 发送前追踪及分析

Agent 必须先完成 [执行流程](agent-workflow.md) 的 investigation.next_actions 并 enrich。send 在联网或变更投递状态前拒绝尚未执行的追踪任务。失败、空结果或定位信息缺失可以形成明确说明局限的通知；不能用普通 REQUEST_COMPLETED、HTTP 状态或 gateway_code 代替实际错误。

analysis-file 以真实 event_id 为键，支持 certainty、judgment、recommendation、内部 evidence 以及可选 classification。certainty 取 confirmed/suspected/unknown。同一错误组基于全部证据写一份分析和建议，并在各成员下复用相同文本及 certainty，证据引用分别填写。render 发现不同分析或覆盖不全时提示 Agent 统一，不任选某条代表整组；未提供分析时显示原因待确认。按分类协议修正预览后再 send，检查返回的 analysis_issues。脚本不调用模型。`render` 可预览待追踪事件，但预览不能 ack。

其他宿主发送工具仍须使用 Skill 的脱敏渲染结果；跨轮次延后和分段回执由内置 send 实现。采用延后策略时不要在外部对整批执行 ack。

## 授权信息处理

查询阶段允许 Agent 得到完整授权值。云访问凭据通过环境变量配置，请求 Header 内的授权值可用于精确检索。

发布时统一脱敏，包括授权字段、错误原文、日志片段和分析引用。默认保留 Key 前 4 位和后 4 位，中间为八个星号；长度不超过 12 的值全部隐藏。`Bearer` / `Basic` 等认证类型保留。支持结构化和常见文本形式的 Authorization、X-API-Key、auth_key、api_key、license_key 等字段。

`query` 输出是内部分析材料，可能含原文；`render` 输出才是可发送文本。`scan` 队列保存脱敏诊断字段和授权 HMAC 分组指纹，不保存原始请求/响应或授权 Key 明文；补查完整授权时使用 query/trace。分析使用了额外原始日志时，把该 JSON 传给 `render` 或 `send` 的 `--secrets-input`，用于识别分析中可能再次出现的完整凭据。它不会被直接附加到消息。

不因安装 skill 自动发测试消息。实际调用 send 的目标、内容和任务范围需要在用户的已有发送授权内。官方自定义机器人文档入口：https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
