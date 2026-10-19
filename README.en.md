# Cloud Log Assistant

[简体中文](README.md)

An agent skill for querying and analyzing logs across cloud platforms. It can query Azure Log Analytics and Volcengine TLS, correlate request traces, enrich events with context, and prepare redacted alerts.

The model runs in the host agent. Scripts produce executable follow-up tasks; the agent retrieves real logs, combines evidence, and analyzes the cause.

## Features

- Query multiple cloud platforms, accounts, regions, and services while preserving each log record's source.
- Parse service-specific log formats, correlate requests and tasks, and enrich events with evidence and uncertainty clearly identified.
- Direct the agent through `investigation.next_actions` to retrieve request context, including INFO and DEBUG records omitted from the initial scan.
- Store scan events in a local persistent queue and send consolidated alerts through Feishu custom bots.
- Let the host agent classify errors from evidence; validate proposals and group within matching service, environment, model, key and upstream scopes. Analysis wording does not define group identity.
- Send compact Feishu cards with report IDs for complete details. Python redacts log fields and agent-written summaries and recommendations.
- Delay repeated findings until 15 minutes after the previous confirmed notification. New groups, higher severity and a pending-log threshold trigger earlier delivery. Deferred events remain pending; interrupted card delivery resumes its saved progress.
- Read credentials from environment variables and mask authorization keys in notification output.
- Extend the skill with additional cloud log providers and service log formats.

## Quick start

Use Python 3.10 or later provided by the host. All built-in capabilities use the standard library; no virtual environment, pip installation, or SDK is required. From the repository root, check Python and copy the example configuration:

Windows PowerShell:

```powershell
py -3 --version
Copy-Item .env.example .env
Copy-Item assets/connections.example.json assets/connections.json
```

macOS / Linux:

```sh
python3 --version
cp .env.example .env
cp assets/connections.example.json assets/connections.json
```

Edit `.env` and `assets/connections.json` with the required cloud credentials, enabled connections, and log resources. Then validate the local configuration and query the last five minutes of logs:

```text
PYTHON scripts/cloud_logs.py doctor --config assets/connections.json --env-file .env
PYTHON scripts/cloud_logs.py query --config assets/connections.json --env-file .env --since 5m --output runtime/query.json
```

Replace `PYTHON` with an available interpreter or launcher, such as `py -3` on Windows, `python3` on macOS / Linux, or the interpreter exposed by your Dify executor. Cloud queries require valid settings and credentials. `doctor` checks local configuration, not cloud connectivity. `virtual_environment=false` is informational and does not prevent readiness.

## Agent workflow

`scan/query → read investigation → agent executes trace → enrich → agent classifies/analyzes → Skill redacts/renders → send when authorized`

Trace tasks specify the connection, Topic/Workspace, and a time range around the event. Feed empty, failed, and truncated results into enrich as well, so attempts do not become endless query loops. Error evidence and request completeness are evaluated separately; ordinary completion logs never substitute for error details. `send` rejects batches with unexecuted trace tasks. See the [execution protocol and agent prompt](references/agent-workflow.md).

## Dify ZIP distribution

```text
PYTHON scripts/package_skill.py
```

This generates `dist/dify/cloud-log-assistant.zip` with SKILL.md at its root. If your Dify interface offers Skill ZIP import, upload the file and enable the skill for the agent. Use the prompt at the end of the workflow reference. The artifact is an Agent Skill ZIP.

In Dify, use the existing Python execution tool directly. Do not create or activate a virtual environment or run pip. The ZIP contains no dependency installation manifest; Volcengine TLS now uses HTTPS requests signed with the standard library.

The executor needs access to Skill files, Python 3.10+ script execution, cloud log endpoints, and injected credentials. Queue workflows also need persistent state storage. Uploading the ZIP does not grant these permissions. Packaging excludes private configuration, real credentials, virtual environments, tests, and runtime data. Report missing execution capabilities instead of repeatedly attempting installation. Verify Dify import and cloud connectivity in your deployment.

## Documentation

- [Configuration and commands](references/configuration.md)
- [Agent execution workflow and prompt](references/agent-workflow.md)
- [Log correlation rules](references/correlation.md)
- [Alert format and notification channels](references/notifications.md)
- [Agent error classification protocol](references/notification-classification.md)
- [Extending the skill](references/extending.md)
- [Azure Log Analytics](references/azure.md) · [Volcengine TLS](references/volcengine.md) · [plugin-api log rules](references/plugin-api.md)

## Credentials and runtime data

Do not commit real credentials. `.env`, virtual environments, and query results, state databases, and previews under `runtime/` are excluded by `.gitignore`. Query results may contain raw logs and complete authorization fields; keep them local for analysis and use the redacted output from `render` or `send` for external notifications.

Scheduled scans and due repeat summaries are driven by the existing host scheduler. This skill does not create schedules or poll continuously. Per-channel repeat_interval_seconds defaults to 900 and repeat_escalation_count defaults to 100 pending log records; set either to 0 to disable that rule. Repeat controls apply to the built-in send command with a persistent outbox.

End the run when available_count is zero, even if pending_count/deferred_count is nonzero. Retrieve card details with `PYTHON scripts/cloud_logs.py report --state-dir ABSOLUTE_STATE_DIR --report-id CARD_REPORT_ID`. See the notification reference for delivery rules and limitations.

When `scan` runs without explicit time arguments, it stores a completion checkpoint in the state directory and retries failed or truncated intervals on the next run. Acknowledged queue records are retained by default. To apply your retention policy, run `PYTHON scripts/cloud_logs.py purge --state-dir ABSOLUTE_STATE_DIR --retention-days 90`.
