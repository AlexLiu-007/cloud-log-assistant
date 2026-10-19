# 配置与命令

## 使用宿主 Python，无需安装依赖

Python 版本至少 3.10。Azure、火山 TLS、飞书、解析与队列均只使用 Python 标准库。直接使用宿主提供的解释器，不创建或激活虚拟环境，不运行 pip，不安装 SDK。已有虚拟环境也能使用，但不作为运行条件。

Windows PowerShell，在 skill 目录执行：

```powershell
py -3 --version
py -3 scripts/cloud_logs.py doctor --config assets/connections.example.json --env-file .env
```

macOS / Linux：

```sh
python3 --version
python3 scripts/cloud_logs.py doctor --config assets/connections.example.json --env-file .env
```

下文 `PYTHON` 表示已经确认可用的 Python 3.10+ 解释器或启动命令，不是要求设置的环境变量。Dify 中优先使用工具明确提供的执行入口和解释器，不能假设存在某台用户电脑上的 `.venv` 路径。

### Dify 的执行步骤

1. 确认实际工具能读取上传的 Skill 文件并执行 Python 脚本。工具提供 Python 调用入口时直接使用；只有提供 Shell 时才运行上述命令。不要猜测未提供的工具名称。
2. 使用现有解释器运行 `doctor`，检查连接与凭据。`virtual_environment=false` 是正常信息；仅看最终 ready 及具体缺失项，不将其当成虚拟环境异常。doctor 不联网验证权限。
3. 从管理员配置的执行环境读取凭据，输出文件放在宿主允许的可写目录。单次 query/trace/enrich 不需要持久队列；scan/claim/send 的队列流程需要固定、可写的持久状态目录。
4. 解释器、文件访问或云端网络不可用时，报告实际缺少的能力。不要循环创建 venv、尝试 pip 安装或切换系统路径。仅能执行孤立代码片段、无法访问 Skill 文件的节点，不能直接当作这个文件型 Skill 的完整执行器。

分发 ZIP 不携带依赖安装清单。旧版安装包若仍提示安装 volcengine，应更新实际挂载的 Skill 包和已有任务中缓存的安装指令。

## 连接配置

从 `assets/connections.example.json` 创建自己的配置。样例预置 Azure `.ai` 与火山 `.cn` 网关连接，并提供默认未启用的 `volc-plugin` 连接；这些是业务样例，不限制其他服务。

plugin-api 使用 `assets/profiles/plugin-api.json`。设置 `VOLC_PLUGIN_LOG_TOPIC_IDS=["PLUGIN_TOPIC_ID"]` 并启用对应连接，即可与其他启用的连接一起查询、合并推送；也可使用内联 topic_ids。不同格式的 Topic 使用独立连接/profile，共用火山凭据。详见 [plugin-api 服务规则](plugin-api.md)。

- 配置的 `connections` 为数组。每个连接有唯一 `id`、`provider`、`service`、`environment`；支持多个云，也支持同一云的多个账号、区域和服务。
- `profile` 和 `query_file` 相对路径以配置文件目录为基准。移动配置时同步调整引用，或使用绝对路径。
- `content_fields` 可指定云返回行中正文所在的路径列表，默认依次识别 FullLog、LogMessage、__content__、message；其他数据格式结合服务 profile 适配。
- `*_env` 指定环境变量名，可为每个连接使用不同前缀。例如第二套 Azure 使用 `AZURE_B_TENANT_ID` 等变量。
- 输出脱敏会按环境变量配置字段名识别 `api_key_env`、`password_env`、`authorization_env` 等凭据。自定义适配器使用非标准字段名时，在配置根级 `redact_env` 中列出凭据环境变量名，例如 `"redact_env": ["CUSTOM_PROVIDER_CREDENTIAL"]`。
- 云端凭据不放进 JSON/skill。 `.env.example` 只列空变量；本地 `.env` 通过 `--env-file` 明确加载，不搜索其他项目环境文件，不执行变量展开或 shell 代码。已有进程环境优先。
- `window_seconds` 是未指定时间时的查询窗口；样例 300 秒。`output_timezone` 是报告时区，样例 `+08:00`。CLI 的绝对开始/结束时间必须含时区。
- `trace_window_seconds` 是追踪任务围绕异常时间向前、向后各扩展的秒数，默认 300，必须为 1–86400 的整数；与 scan 查询窗口独立。
- 默认查询及巡检只排除 INFO、DEBUG，不按状态码或路径排除。级别缺失保留。上下文查询 `--all-levels` 及 `trace` 包含所有级别。
- `max_rows` 表示每个资源本轮最多取回的原始行数（筛选前），火山数组中每个 Topic 独立计算；`max_pages` 是每个 Topic 的页数上限。达到上限保留已取数据并标记不完整。不要将有上限的结果当作全量计数。
- 默认在客户端从元数据/前缀判断级别，避免不同格式造成服务端误过滤。已确认结构化字段后可以使用自定义查询提高效率，再保留本地级别复核。
- `query_file` 可指向用户维护的 KQL/TLS 查询文件。支持 `{{start}}`、`{{end}}`（UTC ISO 时间）、`{{limit}}`（数字）、`{{term}}`（已引用的字符串）。若调用带检索词，自定义文件必须有 `{{term}}`。调用者负责使用正确的云查询语法。

## 常用命令

命令在 skill 目录执行；从其他目录调用时使用脚本、配置、输出的绝对路径。每个命令可用 `--help` 查看参数。

```text
PYTHON scripts/cloud_logs.py doctor --config assets/connections.example.json --env-file .env
PYTHON scripts/cloud_logs.py query --config assets/connections.example.json --env-file .env --since 5m --output runtime/query.json
PYTHON scripts/cloud_logs.py query --config assets/connections.example.json --env-file .env --connection azure-ai --connection volc-cn --start 2026-10-17T11:00:00+08:00 --end 2026-10-17T11:05:00+08:00 --output runtime/query.json
PYTHON scripts/cloud_logs.py query --config assets/connections.example.json --env-file .env --term-env REQUEST_AUTH_KEY --since 1h --all-levels --output runtime/key-search.json
PYTHON scripts/cloud_logs.py trace --config assets/connections.example.json --env-file .env --id-field request_id --id-value REQUEST_ID --since 1h --output runtime/trace.json
PYTHON scripts/cloud_logs.py plan --input runtime/query.json --trace-all --trace-window-seconds 600 --output runtime/planned.json
PYTHON scripts/cloud_logs.py correlate --input runtime/query.json --anchor EVENT_ID --profile assets/profiles/gateway.json --output runtime/correlation.json
PYTHON scripts/cloud_logs.py render --input runtime/correlation.json --output runtime/notification.txt
```

不传 `--connection` 时查询所有启用连接；重复传入该参数可选任意多个连接。每个连接/Topic 独立认证、查询与报告异常，最多四个资源查询并发。`doctor` 检查 Python 版本、变量、profile 和机器人配置；配置可用返回 ready=true 和退出码 0，缺失或无效返回 ready=false 和退出码 2。它不要求虚拟环境，不验证云端连通性，不显示密钥值。

检索 Key 时可以使用临时环境变量 `REQUEST_AUTH_KEY`，避免命令行直接包含 Key；结果按日志记录返回，不把同一 Key 的所有请求合并。`trace` 先检索，再按解析后的指定字段复核，默认对每个连接分别建立关联。

自动生成的追踪任务使用 `--connection` 和 `--resource` 限定具体 Topic/Workspace，并给出围绕事件时间的 `--start`、`--end`。手工 trace 也支持 `--resource`；无法匹配配置资源时失败，不扩大范围。`plan` 只生成任务、不联网；`--trace-all` 为所有事件要求追踪，已完成的追踪仍可复用。

已有离线日志可用 `normalize --config ... --connection ... --input rows.json --output ...`。输入为原始行数组或 `{"rows": [...]}`。它不联网，且不能证明原始采集是否完整。

查询输出包含 `sources`、`records`、`complete` 和时间范围。`records` 供 Agent 使用，可含完整授权及原文；公开消息使用 `render`/`send`。`event_id` 优先基于云端事件/行 ID；没有来源 ID 时使用来源和原始行指纹，并为同一查询中的完全相同记录分配序号。它不是虚构的请求 ID。

查询、巡检、领取及本地处理结果同时提供首字段 `investigation`。Agent 必须解析完整的 `next_actions`，执行追踪并 enrich，再分析和发送；详细触发条件、状态与可直接使用的提示词见 [Agent 执行流程](agent-workflow.md)。

退出码：0 表示命令完成；2 表示输入/查询/投递失败；3 表示云查询得到不完整结果。批量查询/巡检即使退出码为 2 或 3，已成功获取的记录仍写入输出，应读取结果再处理。

## 单次巡检与投递

为每个巡检任务与通知渠道配置独立、固定的绝对 `CLOUD_LOG_STATE_DIR`；一个渠道内的多个机器人共用它。该目录不随客户端生成的临时工作树或聊天变化。CLI 也接受 `--state-dir`。

不传 `--start`、`--end` 或 `--since` 的 `scan` 会在状态库中为每个连接保存查询起点：首次从配置的默认窗口开始；只有该连接的所有资源都成功且完整时才推进起点。失败或不完整时保留原起点，下次继续查同一段并覆盖停机期间的空档。若达到 `max_rows` / `max_pages`，起点会保持不动；提高上限后重跑即可继续。显式指定时间范围的 scan 不读取或推进自动起点。更改连接的日志筛选配置会建立新的起点范围；需要补查历史时仍应显式指定 `--start`。

```text
PYTHON scripts/cloud_logs.py scan --config assets/connections.example.json --env-file .env --output runtime/scan.json
```

读取 scan 的 `investigation.next_actions`，由 Agent 执行每项追踪，再按下面示例 enrich、分析、render、send。没有待执行任务时，可直接使用 scan.json 进入分析。`render` 原始 scan 只用于初步预览，`send` 会拒绝仍待追踪的事件。

`render` 处理 `scan`/`claim` 输出时传入同一 `--state-dir`，以便从状态库读取授权 Key 的 HMAC 分组指纹并合并重复错误。直接渲染仍包含原始授权值的 query/normalize 结果时，无需该参数；通知只使用脱敏后的授权展示。

`analysis-file` 可省略；没有分析时模板显示“暂无法确认原因”。Agent 补查原始记录后再分析；发布时用 `--secrets-input` 提供补查 JSON，可重复传入多个文件，使错误分析中的授权值也能统一遮挡。`render` 可同时传入 `--config`，识别自定义凭据环境变量。

按 investigation 执行 trace 后，保留原批次补全；失败、空结果及不完整的 trace JSON 也要作为 context：

```text
PYTHON scripts/cloud_logs.py enrich --input runtime/scan.json --context runtime/trace-a.json --context runtime/trace-b.json --output runtime/enriched.json
PYTHON scripts/cloud_logs.py render --config assets/connections.example.json --env-file .env --state-dir ABSOLUTE_STATE_DIR --input runtime/enriched.json --analysis-file runtime/analysis.json --secrets-input runtime/trace-a.json --secrets-input runtime/trace-b.json --output runtime/notification.txt
PYTHON scripts/cloud_logs.py send --config assets/connections.example.json --env-file .env --channel feishu-alerts --input runtime/enriched.json --analysis-file runtime/analysis.json --secrets-input runtime/trace-a.json --secrets-input runtime/trace-b.json
```

enrich 不联网，保留所有事件和 delivery_id。它仅以同一连接、同一资源内的 request_id/trace_id/correlation_id 匹配上下文，并核对查询范围覆盖事件时间。不覆盖原始已知字段；先前借用字段遇到新冲突会撤回。不根据业务 ID 或同一 Key 猜测。补全后的 scan 仍不含完整授权和原始日志。字段来源、脱敏上下文与错误证据保留在 associations 中，卡片错误分析保持简短。

增量补全以最近的 enriched.json 为输入，保留已有证据与 trace_queries。过滤了 INFO/DEBUG 的上下文不算已完成追踪。全级别查询失败或截断会记为已尝试，Agent 按限制分析；成功查询覆盖旧失败范围后可替代旧状态。enrich 不修改队列 payload；保存 trace 文件可供重新领取后复用，必须使用新批次 delivery_id。

`scan` 把符合级别规则的事件写入 SQLite 待发送队列，按事件指纹去重，然后领取一个批次。数据库保存已脱敏的诊断字段和状态目录专用的授权 HMAC 分组指纹，不保存原始请求/响应或授权 Key 明文。默认批次 100 条，可用 `--batch-size` 调整；默认租约 900 秒。历史未投递事件即使已离开当前查询窗口仍会重试。

`send` 按 Key + 模型 → 错误 → request_id 列表展示；模型缺失时按 Key 分组。错误使用 Agent 的有效分类或一致的具体错误字段，状态、来源、上游、路径和阶段不额外拆组。同一错误写一份共同分析，复用到各 event_id 下，分析不一致时预览提示修正。首次、新问题、级别上升和达到阈值时提醒；重复事件可延后汇总，保持待发。每个目标保存事件回执及冻结的卡片分段进度；失败重新 claim 后继续下一段。每条事件的全部目标确认后才最终标记已发送。网络结果不确定的片段重试仍可能重复；Webhook 无法保证恰好一次。详见通知文档。

`pending_count` 包含已领取及未领取的待发事件，`available_count` 是还可领取的数量。成功发送当前批次后，继续处理可领取事件：

```text
PYTHON scripts/cloud_logs.py claim --config assets/connections.example.json --env-file .env --state-dir ABSOLUTE_STATE_DIR --output runtime/next-batch.json
```

claim 只读取本地队列，不查询云端，也不要求云凭据。对新批次继续读取 investigation，追踪或复用有效上下文，再 enrich/分析/send；available_count 为 0、发送失败或达到宿主运行时限时结束本轮，并保留未完成记录。不可把默认每批 100 条当成整轮只有 100 条。

使用宿主其他发送能力时，发送成功再手动确认：

```text
PYTHON scripts/cloud_logs.py ack --env-file .env --delivery-id DELIVERY_ID
PYTHON scripts/cloud_logs.py release --env-file .env --delivery-id DELIVERY_ID
```

预览不得 ack。每个批次只交给一个发送流程；发送器会检查租约和事件集合。状态目录绑定通知渠道，渠道中的多个机器人共用该目录；不同任务或渠道使用独立目录。渠道新增机器人作用于尚未最终确认的事件及后续事件，不回放已全部确认的历史消息。

已确认事件和目标回执默认保留，避免在用户未设定期限时自动删除审计记录。按组织保留策略定期清理已确认的旧数据；该操作不会删除待发送或租约中的事件：

```text
PYTHON scripts/cloud_logs.py purge --state-dir ABSOLUTE_STATE_DIR --retention-days 90
```

`purge` 至少要求保留 1 天；每个巡检/渠道状态目录分别执行。清理后再对旧时间范围做人工补查，可能重新入队并再次通知相同事件。

## 宿主调度说明

定时任务由用户的 Agent 客户端或其他调度器创建，skill 不包含客户端专属调度文件。任务提示词明确：选哪些连接、查询窗口、运行频率、固定状态目录、飞书渠道、发送授权以及失败处理。宿主需能访问现有 Python 解释器、配置和环境变量。

每轮执行 scan，读取成功和失败来源及 `checkpoint` 状态，完成 investigation 的追踪任务、enrich 并填写分析，调用 send，再用 claim 处理剩余队列。无新事件且无错误/不完整结果时保持安静；warning-only 且无事件时，`send` 不推送，但会在命令结果的 `warnings_not_sent` 中返回诊断。失败或不完整结果不能视为健康。自动 scan 从最近一个完整水位继续，显式时间范围不会改变该水位；如果自动范围一直不完整，应提高资源上限或手动拆分时间范围后补查。

## 本地验证

在仓库用 Python 3.10+ 执行 `PYTHON -m unittest discover -s scripts/tests -v`（分发 ZIP 不包含 tests）。可加 `-S` 禁止加载第三方 site-packages，验证免依赖运行。测试使用虚构凭据和模拟接口，不发送真实消息。新增服务先用样本执行 normalize，再按授权验证只读查询；通知目标需在已有授权范围内。Windows 建议加 `-X utf8`，避免中文被系统默认编码误读。

## 通知分类、延后和详情

Agent 的可选 classification 字段见 [分类协议](notification-classification.md)，跨轮次提醒和投递规则见 [通知文档](notifications.md)。每个飞书渠道默认 repeat_interval_seconds=900、repeat_escalation_count=100，可设 0 关闭对应功能。策略只在持久队列的内置 send 上生效。

scan/claim 的 available_count 不含正在等待通知窗口的事件；deferred_count 和 next_notification_at 显示等待量与最早时间。到期需要宿主已有调度再次调用 scan/claim。保存的通知上下文已脱敏，claim 可携带 notification_analyses；新 enrich 会清除旧分析。失败重试时按原连接范围领取，未完成的卡片计划可能要求领取超过当前 batch-size 的完整事件集合。

读取卡片详情：`PYTHON scripts/cloud_logs.py report --state-dir ABSOLUTE_STATE_DIR --report-id CARD_REPORT_ID`。详情存于状态目录 reports/，不包含 raw，已统一脱敏；按宿主日志保留策略管理。
