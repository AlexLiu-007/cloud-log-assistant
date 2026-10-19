# Cloud Log Assistant

[简体中文](README.md)

An agent skill for querying and analyzing logs across cloud platforms. It can query Azure Log Analytics and Volcengine TLS, correlate request traces, enrich events with context, and prepare redacted alerts.

## Features

- Query multiple cloud platforms, accounts, regions, and services while preserving each log record's source.
- Parse service-specific log formats, correlate requests and tasks, and enrich events with evidence and uncertainty clearly identified.
- Store scan events in a local persistent queue and send consolidated alerts through Feishu custom bots.
- Group matching errors for the same service, model, and authorization key in notifications while retaining each source event and delivery receipt separately.
- Read credentials from environment variables and mask authorization keys in notification output.
- Extend the skill with additional cloud log providers and service log formats.

## Quick start

Python 3.10 or later is required. From the repository root, create a dedicated virtual environment and copy the example configuration:

Windows PowerShell:

```powershell
py -3 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r scripts/requirements.txt
Copy-Item .env.example .env
Copy-Item assets/connections.example.json assets/connections.json
```

macOS / Linux:

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install -r scripts/requirements.txt
cp .env.example .env
cp assets/connections.example.json assets/connections.json
```

Edit `.env` and `assets/connections.json` with the required cloud credentials, enabled connections, and log resources. Then validate the local configuration and query the last five minutes of logs:

```text
PYTHON scripts/cloud_logs.py doctor --config assets/connections.json --env-file .env
PYTHON scripts/cloud_logs.py query --config assets/connections.json --env-file .env --since 5m --output runtime/query.json
```

Replace `PYTHON` with the interpreter in this virtual environment: `./.venv/Scripts/python.exe` on Windows or `./.venv/bin/python` on macOS / Linux. Cloud queries require valid connection settings and credentials. `doctor` checks local configuration and dependencies; it does not test cloud connectivity.

## Documentation

- [Configuration and commands](references/configuration.md)
- [Log correlation rules](references/correlation.md)
- [Alert format and notification channels](references/notifications.md)
- [Extending the skill](references/extending.md)
- [Azure Log Analytics](references/azure.md) · [Volcengine TLS](references/volcengine.md) · [plugin-api log rules](references/plugin-api.md)

## Credentials and runtime data

Do not commit real credentials. `.env`, virtual environments, and query results, state databases, and previews under `runtime/` are excluded by `.gitignore`. Query results may contain raw logs and complete authorization fields; keep them local for analysis and use the redacted output from `render` or `send` for external notifications.

Scheduled scans are managed by the host agent or another scheduler. This skill does not create scheduled tasks or poll continuously on its own.

When `scan` runs without explicit time arguments, it stores a completion checkpoint in the state directory and retries failed or truncated intervals on the next run. Acknowledged queue records are retained by default. To apply your retention policy, run `PYTHON scripts/cloud_logs.py purge --state-dir ABSOLUTE_STATE_DIR --retention-days 90`.
