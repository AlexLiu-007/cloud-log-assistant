# 多云日志助手

[English](README.en.md)

多云日志查询与分析 Agent Skill。它可以从 Azure Log Analytics 和火山引擎 TLS 查询日志，关联请求链路、补全事件上下文，并生成脱敏后的告警内容。

## 功能

- 查询多个云平台、账号、区域和服务，并保留每条日志的来源。
- 按服务规则解析日志，关联请求与任务，补查上下文并标明证据或不确定性。
- 将巡检事件写入本地持久队列，并通过飞书自定义机器人发送合并告警。
- 通知按相同服务、模型、授权 Key 和错误合并展示；每条底层事件及投递回执仍独立保存。
- 使用环境变量提供凭据；通知输出会遮挡授权 Key。
- 可扩展其他云日志服务和服务日志格式。

## 快速开始

需要 Python 3.10 或更新版本。在仓库根目录创建独立虚拟环境，并复制配置样例：

Windows PowerShell：

```powershell
py -3 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r scripts/requirements.txt
Copy-Item .env.example .env
Copy-Item assets/connections.example.json assets/connections.json
```

macOS / Linux：

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install -r scripts/requirements.txt
cp .env.example .env
cp assets/connections.example.json assets/connections.json
```

编辑 `.env` 和 `assets/connections.json`，填写所需云平台凭据、启用目标连接，并配置日志资源。然后检查配置并查询最近 5 分钟的日志：

```text
PYTHON scripts/cloud_logs.py doctor --config assets/connections.json --env-file .env
PYTHON scripts/cloud_logs.py query --config assets/connections.json --env-file .env --since 5m --output runtime/query.json
```

将 `PYTHON` 替换为本虚拟环境的解释器路径：Windows 使用 `./.venv/Scripts/python.exe`，macOS / Linux 使用 `./.venv/bin/python`。查询云端需要有效的连接配置和凭据；`doctor` 检查本地配置与依赖，不验证云端连通性。

## 文档

- [配置与命令](references/configuration.md)
- [日志关联规则](references/correlation.md)
- [告警格式与通知渠道](references/notifications.md)
- [接入扩展服务](references/extending.md)
- [Azure Log Analytics](references/azure.md) · [火山引擎 TLS](references/volcengine.md) · [plugin-api 日志规则](references/plugin-api.md)

## 凭据与运行数据

不要把真实凭据提交到仓库。`.env`、虚拟环境和 `runtime/` 中的查询结果、状态数据库及预览文件已列入 `.gitignore`。查询结果可能包含原始日志及完整授权字段，应只在本地分析；对外告警请使用 `render` 或 `send` 生成的脱敏内容。

定时巡检由宿主 Agent 或其他调度器负责；本 Skill 不会自行创建定时任务或常驻轮询。

无显式时间参数的 `scan` 会在状态目录中保存完成水位，失败或截断时下次重查该时间段。已确认的队列记录默认保留；需要按保留策略清理时，使用 `PYTHON scripts/cloud_logs.py purge --state-dir ABSOLUTE_STATE_DIR --retention-days 90`。
