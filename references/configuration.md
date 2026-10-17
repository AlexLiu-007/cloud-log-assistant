# 配置与命令

## 独立 Python 环境（必须）

Python 版本至少 3.10。安装任何依赖前，为本 skill 创建独立虚拟环境，默认位置是 skill 目录下的 `.venv`。禁止使用系统/global pip，也不复用其他项目的虚拟环境。已存在环境时确认它属于本 skill，再使用其解释器。

Windows PowerShell，在 skill 目录执行：

```powershell
py -3 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r scripts/requirements.txt
./.venv/Scripts/python.exe scripts/cloud_logs.py doctor --config assets/connections.example.json --env-file .env
```

macOS / Linux：

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install -r scripts/requirements.txt
./.venv/bin/python scripts/cloud_logs.py doctor --config assets/connections.example.json --env-file .env
```

无需激活环境；始终写解释器完整路径。下文 `PYTHON` 表示这个专属解释器，不是一个要求用户设置的变量。Azure、飞书和本地处理使用标准库；火山需要 requirements 中的 SDK。只有授权执行依赖安装时才安装，不因阅读 skill 自动修改运行环境。

## 连接配置

从 `assets/connections.example.json` 创建自己的配置。样例预置 Azure `.ai` 与火山 `.cn` 网关连接，并提供默认未启用的 `volc-plugin` 连接；这些是业务样例，不限制其他服务。

plugin-api 使用 `assets/profiles/plugin-api.json`。设置 `VOLC_PLUGIN_LOG_TOPIC_IDS=["PLUGIN_TOPIC_ID"]` 并启用对应连接，即可与其他启用的连接一起查询、合并推送；也可使用内联 topic_ids。不同格式的 Topic 使用独立连接/profile，共用火山凭据。详见 [plugin-api 服务规则](plugin-api.md)。

- 配置的 `connections` 为数组。每个连接有唯一 `id`、`provider`、`service`、`environment`；支持多个云，也支持同一云的多个账号、区域和服务。
- `profile` 和 `query_file` 相对路径以配置文件目录为基准。移动配置时同步调整引用，或使用绝对路径。
- `content_fields` 可指定云返回行中正文所在的路径列表，默认依次识别 FullLog、LogMessage、__content__、message；其他数据格式结合服务 profile 适配。
- `*_env` 指定环境变量名，可为每个连接使用不同前缀。例如第二套 Azure 使用 `AZURE_B_TENANT_ID` 等变量。
- 云端凭据不放进 JSON/skill。 `.env.example` 只列空变量；本地 `.env` 通过 `--env-file` 明确加载，不搜索其他项目环境文件，不执行变量展开或 shell 代码。已有进程环境优先。
- `window_seconds` 是未指定时间时的查询窗口；样例 300 秒。`output_timezone` 是报告时区，样例 `+08:00`。CLI 的绝对开始/结束时间必须含时区。
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
PYTHON scripts/cloud_logs.py correlate --input runtime/query.json --anchor EVENT_ID --profile assets/profiles/gateway.json --output runtime/correlation.json
PYTHON scripts/cloud_logs.py render --input runtime/correlation.json --output runtime/notification.txt
```

不传 `--connection` 时查询所有启用连接；重复传入该参数可选任意多个连接。每个连接/Topic 独立认证、查询与报告异常，最多四个资源查询并发。`doctor` 检查变量、profile、依赖和机器人配置；配置可用返回 ready=true 和退出码 0，缺失或无效返回 ready=false 和退出码 2。它不验证云端连通性，不显示密钥值。

检索 Key 时可以使用临时环境变量 `REQUEST_AUTH_KEY`，避免命令行直接包含 Key；结果按日志记录返回，不把同一 Key 的所有请求合并。`trace` 先检索，再按解析后的指定字段复核，默认对每个连接分别建立关联。

已有离线日志可用 `normalize --config ... --connection ... --input rows.json --output ...`。输入为原始行数组或 `{"rows": [...]}`。它不联网，且不能证明原始采集是否完整。

查询输出包含 `sources`、`records`、`complete` 和时间范围。`records` 供 Agent 使用，可含完整授权及原文；公开消息使用 `render`/`send`。记录的 `event_id` 是来源和日志内容的稳定指纹，不是虚构的请求 ID。

退出码：0 表示命令完成；2 表示输入/查询/投递失败；3 表示云查询得到不完整结果。批量查询/巡检即使退出码为 2 或 3，已成功获取的记录仍写入输出，应读取结果再处理。

## 单次巡检与投递

为每个巡检任务与通知渠道配置独立、固定的绝对 `CLOUD_LOG_STATE_DIR`；一个渠道内的多个机器人共用它。该目录不随客户端生成的临时工作树或聊天变化。CLI 也接受 `--state-dir`。

```text
PYTHON scripts/cloud_logs.py scan --config assets/connections.example.json --env-file .env --output runtime/scan.json
PYTHON scripts/cloud_logs.py render --input runtime/scan.json --analysis-file runtime/analysis.json --output runtime/notification.txt
PYTHON scripts/cloud_logs.py send --config assets/connections.example.json --env-file .env --channel feishu-alerts --input runtime/scan.json --analysis-file runtime/analysis.json
```

`analysis-file` 可省略；没有分析时模板显示“暂无法确认原因”。Agent 补查原始记录后再分析；发布时用 `--secrets-input` 提供补查 JSON，可重复传入多个文件，使错误分析中的授权值也能统一遮挡。`render` 可同时传入 `--config`，识别自定义凭据环境变量。

告警缺少路径、模型或错误详情时，先 trace，再保留原批次补全：

```text
PYTHON scripts/cloud_logs.py enrich --input runtime/scan.json --context runtime/trace-a.json --context runtime/trace-b.json --output runtime/enriched.json
PYTHON scripts/cloud_logs.py render --config assets/connections.example.json --env-file .env --input runtime/enriched.json --analysis-file runtime/analysis.json --secrets-input runtime/trace-a.json --secrets-input runtime/trace-b.json --output runtime/notification.txt
PYTHON scripts/cloud_logs.py send --config assets/connections.example.json --env-file .env --channel feishu-alerts --input runtime/enriched.json --analysis-file runtime/analysis.json --secrets-input runtime/trace-a.json --secrets-input runtime/trace-b.json
```

enrich 不联网，保留所有事件和 delivery_id。它仅以同一连接、同一资源内的 request_id/trace_id/correlation_id 匹配上下文；不覆盖已知字段，不补入冲突值，不根据业务 ID 或同一 Key 猜测。补全后的 scan 仍不含完整授权和原始日志。字段来源证据保留在本地 associations 中，卡片错误分析保持简短。

`scan` 把符合级别规则的事件写入 SQLite 待发送队列，按事件指纹去重，然后领取一个批次。数据库保存已脱敏的诊断字段，不保存原始请求/响应。默认批次 100 条，可用 `--batch-size` 调整；默认租约 900 秒。历史未投递事件即使已离开当前查询窗口仍会重试。

`send` 合并同一批次的所有来源，向渠道配置的全部机器人发送。一个机器人失败会继续尝试其他机器人；队列为每个事件保存各目标的成功回执，重试跳过已完成的目标。全部目标成功才确认事件；失败释放租约。发送期间逐段续租，避免长批次租约过期后被重复领取。没有事件、没有来源异常时保持安静。单个机器人内部多段消息部分成功后的重试仍可能重复片段；Webhook 无法保证恰好一次投递。

`pending_count` 包含已领取及未领取的待发事件，`available_count` 是还可领取的数量。成功发送当前批次后，继续处理可领取事件：

```text
PYTHON scripts/cloud_logs.py claim --config assets/connections.example.json --env-file .env --state-dir ABSOLUTE_STATE_DIR --output runtime/next-batch.json
```

claim 只读取本地队列，不查询云端，也不要求云凭据。继续使用补查/分析/send 流程；available_count 为 0、发送失败或达到宿主运行时限时结束本轮，并保留未完成记录。不可把默认每批 100 条当成整轮只有 100 条。

使用宿主其他发送能力时，发送成功再手动确认：

```text
PYTHON scripts/cloud_logs.py ack --env-file .env --delivery-id DELIVERY_ID
PYTHON scripts/cloud_logs.py release --env-file .env --delivery-id DELIVERY_ID
```

预览不得 ack。每个批次只交给一个发送流程；发送器会检查租约和事件集合。状态目录绑定通知渠道，渠道中的多个机器人共用该目录；不同任务或渠道使用独立目录。渠道新增机器人作用于尚未最终确认的事件及后续事件，不回放已全部确认的历史消息。已发送指纹和目标回执不会自动删除，保留期按运维需求管理。

## 宿主调度说明

定时任务由用户的 Agent 客户端或其他调度器创建，skill 不包含客户端专属调度文件。任务提示词明确：选哪些连接、查询窗口、运行频率、固定状态目录、飞书渠道、发送授权以及失败处理。宿主需能访问专属解释器、配置和环境变量。

每轮执行 scan，读取成功和失败来源，必要时补查、enrich 并填写分析，调用 send，再用 claim 处理剩余队列。无新事件保持安静；失败或不完整结果不能视为健康。查询始终使用确定的最近窗口，不自动扩大窗口补采停机期间日志；如需补采，由用户指定范围。

## 本地验证

在 skill 专属环境执行 `PYTHON -m unittest discover -s scripts/tests -v`。测试使用虚构凭据和模拟接口，不发送真实消息。新增服务先用真实样本执行 normalize，确认级别、时间、标识和错误字段，再验证实际只读查询；通知目标需在已有授权范围内。Windows 建议使用 `python.exe -X utf8` 运行外部检查工具，避免中文文件被系统默认编码误读。
