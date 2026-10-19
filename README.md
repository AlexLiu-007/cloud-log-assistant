# 多云日志助手

[English](README.en.md)

多云日志查询与分析 Agent Skill。它可以从 Azure Log Analytics 和火山引擎 TLS 查询日志，关联请求链路、补全事件上下文，并生成脱敏后的告警内容。

模型运行在宿主 Agent 侧。脚本生成可执行的追踪任务，Agent 按任务取回真实日志、合并证据并分析原因。

## 功能

- 查询多个云平台、账号、区域和服务，并保留每条日志的来源。
- 按服务规则解析日志，关联请求与任务，补查上下文并标明证据或不确定性。
- 通过 `investigation.next_actions` 指挥 Agent 继续追踪；初次扫描没有错误详情时，补查包含 INFO、DEBUG 的同请求日志。
- 将巡检事件写入本地持久队列，并通过飞书自定义机器人发送合并告警。
- 通知按相同服务、模型、授权 Key 和错误合并展示；每条底层事件及投递回执仍独立保存。
- 使用环境变量提供凭据；通知输出会遮挡授权 Key。
- 可扩展其他云日志服务和服务日志格式。

## 快速开始

需要宿主提供 Python 3.10 或更新版本。全部内置能力只用标准库，无需虚拟环境、pip 或 SDK。在仓库根目录确认 Python 可用，并复制配置样例：

Windows PowerShell：

```powershell
py -3 --version
Copy-Item .env.example .env
Copy-Item assets/connections.example.json assets/connections.json
```

macOS / Linux：

```sh
python3 --version
cp .env.example .env
cp assets/connections.example.json assets/connections.json
```

编辑 `.env` 和 `assets/connections.json`，填写所需云平台凭据、启用目标连接，并配置日志资源。然后检查配置并查询最近 5 分钟的日志：

```text
PYTHON scripts/cloud_logs.py doctor --config assets/connections.json --env-file .env
PYTHON scripts/cloud_logs.py query --config assets/connections.json --env-file .env --since 5m --output runtime/query.json
```

将 `PYTHON` 替换为已确认可用的解释器或启动命令，例如 Windows 的 `py -3`、macOS / Linux 的 `python3`，或 Dify 执行器提供的路径。查询云端需要有效配置和凭据；`doctor` 检查本地配置，不验证云端连通性。`virtual_environment=false` 是正常信息，不影响 ready。

## Agent 运行流程

`scan/query → 读取 investigation → Agent 执行 trace → enrich → Agent 分析 → render → 按授权 send`

追踪任务限定具体连接、Topic/Workspace 和异常发生时间附近的范围。空结果、失败和截断结果也要 enrich，避免无限重复查询。错误证据与链路完整性分别判断；普通完成日志不会代替具体错误。`send` 会拒绝仍有未执行追踪任务的批次。完整协议与可复制的 Agent 提示词见 [Agent 执行流程](references/agent-workflow.md)。

## Dify ZIP 分发

```text
PYTHON scripts/package_skill.py
```

生成 `dist/dify/cloud-log-assistant.zip`，ZIP 根目录包含 SKILL.md。若你的 Dify 页面提供 Skill ZIP 导入入口，可上传此文件并为 Agent 启用该 Skill；使用流程文档末尾的提示词。这里分发的是 Agent Skill ZIP。

在 Dify 中直接使用现有 Python 执行工具，不创建/激活 `.venv`，不运行 pip。包不包含依赖安装清单；火山 TLS 已改为标准库 HTTPS 签名请求。

执行环境需要 Skill 文件访问、Python 3.10+ 脚本执行、云日志网络和凭据注入；队列巡检另需持久状态目录。上传 ZIP 不会赋予这些权限。包排除私有配置、真实凭据、虚拟环境、测试与运行数据。缺少执行能力时应报告具体缺口，不能反复尝试安装。Dify 实际导入和云端联通需在部署环境中验证。

## 文档

- [配置与命令](references/configuration.md)
- [Agent 执行流程与运行提示词](references/agent-workflow.md)
- [日志关联规则](references/correlation.md)
- [告警格式与通知渠道](references/notifications.md)
- [接入扩展服务](references/extending.md)
- [Azure Log Analytics](references/azure.md) · [火山引擎 TLS](references/volcengine.md) · [plugin-api 日志规则](references/plugin-api.md)

## 凭据与运行数据

不要把真实凭据提交到仓库。`.env`、虚拟环境和 `runtime/` 中的查询结果、状态数据库及预览文件已列入 `.gitignore`。查询结果可能包含原始日志及完整授权字段，应只在本地分析；对外告警请使用 `render` 或 `send` 生成的脱敏内容。

定时巡检由宿主 Agent 或其他调度器负责；本 Skill 不会自行创建定时任务或常驻轮询。

无显式时间参数的 `scan` 会在状态目录中保存完成水位，失败或截断时下次重查该时间段。已确认的队列记录默认保留；需要按保留策略清理时，使用 `PYTHON scripts/cloud_logs.py purge --state-dir ABSOLUTE_STATE_DIR --retention-days 90`。
