#!/usr/bin/env python3
"""Client-independent cloud log CLI. Query data may contain full request credentials."""
from __future__ import annotations

import argparse
import importlib
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cloud_log_common import (LogError, config_connections, digest, get_profile, load_env,
                              parse_time, read_json, validate_connection, write_json, zone)
from cloud_log_correlation import IDENTIFIERS, correlate
from cloud_log_parsing import SECRET_NAMES, key_name, normalize
from cloud_log_reporting import collect_secrets, render
from cloud_log_workflow import plan_investigation, query_status
from cloud_log_state import (advance_scan_cursors, enqueue_and_claim, load_scan_cursors,
                             load_authorization_fingerprints, purge_sent, receipt)


def adapter(connection):
    name = connection.get("provider", "")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise LogError("云平台 provider 必须对应 scripts/providers 中的适配模块")
    try:
        return importlib.import_module("providers." + name)
    except ModuleNotFoundError:
        raise LogError("云平台适配模块不可用：" + name) from None


def window(args, config):
    end = parse_time(args.end) if args.end else datetime.now(timezone.utc)
    if args.start:
        if args.since:
            raise LogError("start 与 since 不能同时使用")
        start = parse_time(args.start)
    else:
        seconds = int(config.get("window_seconds", 300))
        if args.since:
            match = re.fullmatch(r"(\d+)(s|m|h|d)", args.since)
            if not match:
                raise LogError("since 例如 300s、5m、1h")
            seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
        start = end - timedelta(seconds=seconds)
    if start >= end:
        raise LogError("查询结束时间必须晚于开始时间")
    return start, end


def query_one(connection, start, end, term=None, context=False, limit=None):
    source = {"connection_id": connection["id"], "provider": connection["provider"],
              "service": connection.get("service", ""), "environment": connection.get("environment", ""),
              "query_start": start.isoformat(), "query_end": end.isoformat()}
    if connection.get("_topic_id"):
        source["resource"] = connection["_topic_id"]
    try:
        module = adapter(connection)
        if hasattr(module, "resource_id"):
            try:
                source["resource"] = module.resource_id(connection)
            except LogError:
                pass  # The adapter query reports missing/invalid configuration.
        if context:
            connection = {key: value for key, value in connection.items() if key != "query_file"}
        validate_connection(connection)
        count = limit if limit is not None else int(connection.get("max_rows", 10000))
        if count < 1 or int(connection.get("max_pages", 100)) < 1:
            raise LogError("max_rows、max_pages 必须大于零")
        profile = get_profile(connection)
        response = module.query(connection, start, end, count, term=term, context=context)
        records, warnings = [], list(response.get("warnings", []))
        parse_failures, missing_levels = 0, 0
        occurrences = {}
        for row in response["rows"]:
            try:
                record = normalize(row, connection, profile, response.get("resource", ""))
            except Exception as exc:
                parse_failures += 1
                record = {"event_id": digest([connection["id"], response.get("resource"), row]),
                          "connection_id": connection["id"], "provider": connection["provider"],
                          "service": connection.get("service", ""), "environment": connection.get("environment", ""),
                          "resource": response.get("resource", ""), "timestamp": "", "level": "",
                          "raw": row, "message": "日志解析失败（" + type(exc).__name__ + "）",
                          "parse_warnings": ["未解析记录，需调整服务规则"]}
            if record.get("timestamp") and not start <= parse_time(record["timestamp"]) < end:
                continue
            if not context and record.get("level") in {"INFO", "DEBUG"}:
                continue
            if not record.get("level"):
                missing_levels += 1
            fingerprint = record["event_id"]
            occurrence = occurrences.get(fingerprint, 0)
            occurrences[fingerprint] = occurrence + 1
            if occurrence:
                record["event_id"] = digest([fingerprint, "duplicate", occurrence])
            records.append(record)
        if parse_failures:
            warnings.append(f"{parse_failures} 条记录解析失败，原始数据保留在查询结果中")
        if missing_levels:
            warnings.append(f"{missing_levels} 条记录缺少日志级别，已保留")
        source.update(status="ok", complete=bool(response["complete"]) and not parse_failures,
                      warnings=warnings, resource=response.get("resource", ""),
                      query_kind=response.get("query_kind", "raw"),
                      scanned=response.get("scanned", len(response["rows"])), matched=len(records))
        return source, records
    except Exception as exc:
        error = str(exc) if isinstance(exc, LogError) else "查询异常（" + type(exc).__name__ + "），检查连接配置"
        source.update(status="failed", complete=False, error=error, warnings=[])
        return source, []


def query_all(args, config, connections, *, term=None, context=False, period=None,
              per_connection_starts=None):
    start, end = period or window(args, config)
    per_connection_starts = per_connection_starts or {}
    def run(connection):
        return query_one(connection, per_connection_starts.get(connection["id"], start),
                         end, term, context, args.max_rows)
    jobs, results = [], []
    resource = getattr(args, "resource", None)
    for connection in connections:
        try:
            module = adapter(connection)
            expanded = module.expand_connections(connection) if hasattr(module, "expand_connections") else [connection]
            if resource:
                if not hasattr(module, "resource_id"):
                    raise LogError("此适配器尚未实现按资源追踪")
                expanded = [item for item in expanded if module.resource_id(item) == resource]
                if not expanded:
                    raise LogError("指定资源不在所选连接的可用配置中，未扩大查询范围")
            jobs.extend(expanded)
        except Exception as exc:
            error = str(exc) if isinstance(exc, LogError) else "连接展开失败（" + type(exc).__name__ + "）"
            results.append(({"connection_id": connection["id"], "provider": connection.get("provider", ""),
                             "resource": resource or "",
                             "query_start": per_connection_starts.get(connection["id"], start).isoformat(),
                             "query_end": end.isoformat(), "status": "failed", "complete": False,
                             "error": error, "warnings": []}, []))
    with ThreadPoolExecutor(max_workers=min(len(jobs), 4) or 1) as pool:
        results.extend(pool.map(run, jobs))
    order = {connection["id"]: index for index, connection in enumerate(connections)}
    results.sort(key=lambda result: (order[result[0]["connection_id"]], result[0].get("resource", "")))
    sources = [source for source, _ in results]
    records = [record for _, rows in results for record in rows]
    records.sort(key=lambda record: (record.get("timestamp") or "", record["connection_id"], record["event_id"]))
    query_start = min((per_connection_starts.get(connection["id"], start) for connection in connections),
                      default=start)
    return {"schema_version": 1, "kind": "query", "query_start": query_start.isoformat(), "query_end": end.isoformat(),
            "output_timezone": config.get("output_timezone", "+00:00"),
            "complete": all(source["complete"] for source in sources),
            "selection": "all_levels" if context else "exclude_INFO_DEBUG",
            "sources": sources, "records": records}


def trace_result(document, field, value, cross_connections):
    document["records"] = [r for r in document["records"] if r.get(field) == value]
    for source in document.get("sources", []):
        source["candidate_matches"] = source.get("matched", 0)
        source["matched"] = sum(r["connection_id"] == source["connection_id"] and
                                r.get("resource", "") == source.get("resource", "") for r in document["records"])
    groups = {}
    for record in document["records"]:
        key = "all" if cross_connections else (record["connection_id"], record.get("resource", ""))
        groups.setdefault(key, []).append(record)
    associations = []
    for records in groups.values():
        scoped_sources = [s for s in document.get("sources", []) if cross_connections or
                          (s["connection_id"], s.get("resource", "")) ==
                          (records[0]["connection_id"], records[0].get("resource", ""))]
        complete = all(s.get("complete") is True for s in scoped_sources) if scoped_sources else document["complete"]
        association = correlate(records, records[0]["event_id"], field=field,
                                cross_connections=cross_connections, complete=complete)
        association["trace_status"] = query_status(document, scoped_sources)
        association["event_ids"] = [r["event_id"] for r in association.pop("records")]
        associations.append(association)
    document.update(kind="trace", associations=associations, matched_identifier={"field": field, "value": value})
    document["trace_summary"] = {"status": query_status(document, document.get("sources", [])),
                                 "matched_records": len(document["records"])}
    return document


def state_dir(args):
    value = args.state_dir or os.getenv("CLOUD_LOG_STATE_DIR")
    if not value:
        raise LogError("配置 CLOUD_LOG_STATE_DIR 或传入 --state-dir")
    return value


def sensitive_env_field(field):
    name = key_name(field[:-4] if field.endswith("_env") else field)
    compact = name.replace("_", "")
    markers = ("authorization", "authkey", "apikey", "token", "secret", "password",
               "accesskey", "cookie", "webhook", "licensekey", "credential")
    return name in SECRET_NAMES or compact in SECRET_NAMES or any(marker in compact for marker in markers)


def runtime_secrets(config=None):
    config = config or {}
    names = {"AZURE_LOG_CLIENT_SECRET", "VOLC_ACCESS_KEY", "VOLC_SECRET_KEY", "VOLC_SESSION_TOKEN",
             "FEISHU_WEBHOOK", "FEISHU_SECRET"}
    names.update(name for name in config.get("redact_env", [])
                 if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))
    for connection in config.get("connections", []):
        names.update(value for key, value in connection.items()
                     if key.endswith("_env") and sensitive_env_field(key) and isinstance(value, str))
    for channel in (config or {}).get("notifications", {}).values():
        names.update(channel.get(key) for key in ("webhook_env", "secret_env")
                     if isinstance(channel.get(key), str) and channel.get(key))
    values = {os.environ[name] for name in names if os.getenv(name)}
    return values | collect_secrets(list(values))


def checkpoint_scopes(connections):
    """Fingerprint source selection without storing credential values in the state DB."""
    scopes = {}
    ignored = {"_base_dir", "_topic_id", "enabled", "environment", "max_rows", "max_pages",
               "profile", "service", "timeout_seconds"}
    for connection in connections:
        scope = {key: value for key, value in connection.items()
                 if key not in ignored and not key.startswith("_") and not sensitive_env_field(key)}
        for key, env_name in connection.items():
            if (key.endswith("_env") and not sensitive_env_field(key) and isinstance(env_name, str) and
                    any(word in key for word in ("workspace", "topic", "resource", "log_group", "log_store",
                                                 "project", "tenant", "account", "subscription", "database",
                                                 "index", "stream", "organization"))):
                scope["resolved_" + key] = os.getenv(env_name, "")
        query_file = connection.get("query_file")
        if query_file:
            path = Path(query_file)
            if not path.is_absolute():
                path = Path(connection["_base_dir"]) / path
            try:
                scope["query_file_content"] = digest(path.read_text(encoding="utf-8-sig"))
            except OSError:
                scope["query_file_content"] = "unavailable"
        scopes[connection["id"]] = digest(scope)
    return scopes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "query", "trace", "scan", "claim", "normalize", "send"):
        command = sub.add_parser(name)
        command.add_argument("--config", required=True)
        command.add_argument("--env-file")
        command.add_argument("--connection", action="append", help="Repeat to select multiple connections")
        command.add_argument("--output")
        if name in {"query", "trace", "scan"}:
            command.add_argument("--start")
            command.add_argument("--end")
            command.add_argument("--since")
            command.add_argument("--max-rows", type=int)
        if name == "query":
            choice = command.add_mutually_exclusive_group()
            choice.add_argument("--term")
            choice.add_argument("--term-env")
            command.add_argument("--all-levels", action="store_true")
        if name == "trace":
            command.add_argument("--id-field", choices=IDENTIFIERS, default="request_id")
            command.add_argument("--id-value", required=True)
            command.add_argument("--cross-connections", action="store_true")
            command.add_argument("--resource", help="Query only this configured Topic/workspace")
        if name in {"scan", "claim"}:
            command.add_argument("--state-dir")
            command.add_argument("--lease-seconds", type=int, default=900)
            command.add_argument("--batch-size", type=int, default=100)
        if name in {"normalize", "send"}:
            command.add_argument("--input", required=True)
        if name == "normalize":
            command.add_argument("--all-levels", action="store_true")
        if name == "send":
            command.add_argument("--channel", required=True)
            command.add_argument("--analysis-file")
            command.add_argument("--secrets-input", action="append")
            command.add_argument("--state-dir")
    command = sub.add_parser("correlate")
    command.add_argument("--input", required=True)
    command.add_argument("--anchor", required=True)
    command.add_argument("--id-field", choices=IDENTIFIERS)
    command.add_argument("--cross-connections", action="store_true")
    command.add_argument("--window-seconds", type=int, default=30)
    command.add_argument("--profile")
    command.add_argument("--output")
    command = sub.add_parser("render")
    command.add_argument("--config", help="Optional config for custom credential environment names")
    command.add_argument("--input", required=True)
    command.add_argument("--analysis-file")
    command.add_argument("--secrets-input", action="append", help="Repeat raw query JSON files used only to recognize secrets")
    command.add_argument("--env-file")
    command.add_argument("--state-dir", help="State directory used to recover scan-only grouping metadata")
    command.add_argument("--output")
    command = sub.add_parser("enrich")
    command.add_argument("--input", required=True)
    command.add_argument("--context", action="append", required=True, help="Repeat for additional query/trace JSON files")
    command.add_argument("--output", required=True)
    command = sub.add_parser("plan", help="Generate follow-up tasks without querying cloud services")
    command.add_argument("--input", required=True)
    command.add_argument("--output")
    command.add_argument("--trace-all", action="store_true", default=None)
    command.add_argument("--trace-window-seconds", type=int)
    command = sub.add_parser("report", help="Read a redacted notification detail report")
    command.add_argument("--state-dir", required=True)
    command.add_argument("--report-id", required=True)
    command.add_argument("--output")
    for name in ("ack", "release"):
        command = sub.add_parser(name)
        command.add_argument("--state-dir")
        command.add_argument("--delivery-id", required=True)
        command.add_argument("--env-file")
        command.add_argument("--output")
    command = sub.add_parser("purge")
    command.add_argument("--state-dir")
    command.add_argument("--retention-days", type=int, required=True)
    command.add_argument("--env-file")
    command.add_argument("--output")
    args = parser.parse_args()
    load_env(getattr(args, "env_file", None))
    config, connections = ({}, [])
    if getattr(args, "config", None):
        if args.command in {"send", "render"}:
            config = read_json(args.config)
        else:
            config, connections = config_connections(args.config, args.connection)
    if "trace_window_seconds" in config:
        plan_investigation({}, window_seconds=config["trace_window_seconds"])
    if args.command == "doctor":
        from cloud_log_delivery import channel_settings
        zone(config.get("output_timezone", "+00:00"))
        results = []
        for connection in connections:
            try:
                validate_connection(connection)
                info = adapter(connection).configuration_status(connection)
                get_profile(connection)
                ready = all(info.get("environment", {}).values()) and info.get("dependency_available", True)
                results.append({"connection_id": connection["id"], **info, "ready": ready})
            except Exception as exc:
                results.append({"connection_id": connection["id"], "ready": False,
                                "error": str(exc) if isinstance(exc, LogError) else type(exc).__name__})
        notifications = {}
        for key, channel in config.get("notifications", {}).items():
            try:
                targets, _, _ = channel_settings(channel)
                notifications[key] = {"provider": channel["provider"], "ready": True,
                                      "webhook_configured": True, "target_count": len(targets),
                                      "signing_enabled": any(t["secret"] for t in targets)}
            except Exception as exc:
                notifications[key] = {"ready": False, "error": str(exc) if isinstance(exc, LogError) else type(exc).__name__}
        result = {"kind": "configuration", "network_checked": False,
                  "virtual_environment": sys.prefix != sys.base_prefix,
                  "virtual_environment_required": False,
                  "python_executable": sys.executable,
                  "python_version": ".".join(str(value) for value in sys.version_info[:3]),
                  "standard_library_only": True,
                  "connections": results,
                  "notifications": notifications,
                  "ready": sys.version_info >= (3, 10) and all(r["ready"] for r in results) and
                           all(r["ready"] for r in notifications.values())}
    elif args.command in {"query", "trace", "scan"}:
        term = getattr(args, "term", None)
        if getattr(args, "term_env", None):
            term = os.getenv(args.term_env)
            if not term:
                raise LogError("缺少检索词环境变量：" + args.term_env)
        if args.command == "trace":
            if not args.id_value.strip():
                raise LogError("追踪标识不能为空")
            term = args.id_value
        period = window(args, config)
        cursor_state, scopes, starts = None, {}, None
        automatic_scan = args.command == "scan" and not any((args.start, args.end, args.since))
        if automatic_scan:
            cursor_state = state_dir(args)
            scopes = checkpoint_scopes(connections)
            starts = load_scan_cursors(cursor_state, scopes, period[0])
        result = query_all(args, config, connections, term=term,
                           context=args.command == "trace" or getattr(args, "all_levels", False),
                           period=period, per_connection_starts=starts)
        if args.command == "trace":
            result = trace_result(result, args.id_field, args.id_value, args.cross_connections)
        elif args.command == "scan":
            delivery = enqueue_and_claim(state_dir(args), result.pop("records"), [c["id"] for c in connections],
                                         args.lease_seconds, args.batch_size, runtime_secrets(config))
            result.update(kind="scan", **delivery)
            if automatic_scan:
                grouped = {}
                for source in result.get("sources", []):
                    grouped.setdefault(source["connection_id"], []).append(source)
                completed = {connection_id: scope_hash for connection_id, scope_hash in scopes.items()
                             if grouped.get(connection_id) and all(
                                 source.get("status") == "ok" and source.get("complete") is True
                                 for source in grouped[connection_id])}
                advance_scan_cursors(cursor_state, completed, period[1])
                result["checkpoint"] = {"mode": "automatic",
                                         "advanced_connections": sorted(completed),
                                         "held_connections": sorted(set(scopes) - set(completed))}
            else:
                result["checkpoint"] = {"mode": "explicit_range", "advanced_connections": []}
    elif args.command == "claim":
        result = {"kind": "scan", "operation": "claim", "schema_version": 1,
                  "output_timezone": config.get("output_timezone", "+00:00"), "sources": [],
                  **enqueue_and_claim(state_dir(args), [], [c["id"] for c in connections],
                                      args.lease_seconds, args.batch_size)}
    elif args.command == "enrich":
        from cloud_log_enrichment import enrich
        result = enrich(read_json(args.input), [read_json(path) for path in args.context])
    elif args.command == "plan":
        result = read_json(args.input)
        result["investigation"] = plan_investigation(result, window_seconds=args.trace_window_seconds,
                                                      trace_all=args.trace_all)
    elif args.command == "normalize":
        if len(connections) != 1:
            raise LogError("离线解析需用 --connection 选择一个来源")
        data = read_json(args.input)
        rows = data if isinstance(data, list) else data.get("rows")
        if not isinstance(rows, list):
            raise LogError("离线输入需要原始行数组或包含 rows 的对象")
        connection = connections[0]
        profile = get_profile(connection)
        records = [normalize(row, connection, profile, "offline") for row in rows]
        if not args.all_levels:
            records = [r for r in records if r.get("level") not in {"INFO", "DEBUG"}]
        result = {"kind": "normalized", "schema_version": 1, "complete": False,
                  "output_timezone": config.get("output_timezone", "+00:00"), "records": records,
                  "sources": [{"connection_id": connection["id"], "warnings": ["离线样本的采集范围和完整性未确认"]}]}
    elif args.command == "correlate":
        result = read_json(args.input)
        profile = read_json(args.profile) if args.profile else {}
        association = correlate(result.get("records", result.get("alerts", [])), args.anchor,
                                field=args.id_field, cross_connections=args.cross_connections,
                                window_seconds=profile.get("heuristic_window_seconds", args.window_seconds),
                                heuristic_fields=profile.get("heuristic_fields", ("path", "actor", "model")),
                                complete=query_status(result, result.get("sources", [])) == "complete")
        result = {key: value for key, value in result.items() if key not in {"records", "alerts", "associations"}}
        result.update(kind="correlation", records=association.pop("records"), association=association)
    elif args.command == "render":
        document = read_json(args.input)
        analyses = read_json(args.analysis_file) if args.analysis_file else {}
        secrets = runtime_secrets(config)
        if args.secrets_input:
            for path in args.secrets_input:
                secrets |= collect_secrets(read_json(path))
        grouping_state = args.state_dir or os.getenv("CLOUD_LOG_STATE_DIR")
        fingerprints = (load_authorization_fingerprints(grouping_state, [
            record["event_id"] for record in document.get("alerts", document.get("records", []))])
                        if grouping_state else {})
        text = render(document, analyses, secrets, fingerprints)
        if args.output:
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 0
    elif args.command == "send":
        from cloud_log_delivery import send_document
        document = read_json(args.input)
        secrets = runtime_secrets(config)
        if args.secrets_input:
            for path in args.secrets_input:
                secrets |= collect_secrets(read_json(path))
        result = send_document(config, args.channel, document,
                               read_json(args.analysis_file) if args.analysis_file else {},
                                state_dir(args) if document.get("delivery_id") or args.state_dir else None, secrets)
    elif args.command == "report":
        from cloud_log_notify_state import read_report
        result = read_report(args.state_dir, args.report_id)
    elif args.command == "purge":
        result = purge_sent(state_dir(args), args.retention_days)
    else:
        result = receipt(state_dir(args), args.delivery_id, args.command == "ack")
    if args.command in {"query", "trace", "scan", "claim", "normalize", "correlate", "enrich"}:
        result["investigation"] = plan_investigation(result, window_seconds=config.get("trace_window_seconds"))
    if "investigation" in result:
        result = {"investigation": result["investigation"],
                  **{key: value for key, value in result.items() if key != "investigation"}}
    write_json(result, args.output)
    if args.command == "doctor" and not result["ready"]:
        return 2
    if any(source.get("status") == "failed" for source in result.get("sources", [])):
        return 2
    return 3 if result.get("complete") is False and args.command in {"query", "trace", "scan"} else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except Exception as exc:
        message = str(exc) if isinstance(exc, LogError) else "执行失败（" + type(exc).__name__ + "）；检查输入和配置"
        print(message, file=sys.stderr)
        raise SystemExit(2)
