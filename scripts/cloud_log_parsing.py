"""Service-aware parsing; full authorization remains available until publication."""
from __future__ import annotations

import ast
import json
import re

from cloud_log_common import digest, parse_time

MISSING = (None, "", "-", "None", "null")
FIELDS = {
    "timestamp": ["TimeGenerated", "__time__", "timestamp", "time", "@timestamp"],
    "level": ["LogLevel", "level.name", "level", "severity", "log_level", "severityText"],
    "request_id": ["request_id", "RequestId", "requestId"],
    "trace_id": ["trace_id", "traceId", "TraceId", "OperationId", "operation_Id"],
    "correlation_id": ["correlation_id", "correlationId"],
    "business_id": ["job_id", "task_id", "message_id", "order_id"],
    "actor": ["user_id", "user_name", "app_id", "application_id"],
    "authorization": ["authorization", "auth_key", "api_key", "headers.Authorization", "headers.X-API-Key"],
    "method": ["method", "http_method", "request.method"],
    "path": ["normalized_path", "path", "api_path", "request.path"],
    "model": ["effective_model_id", "request.model_route.effective_model_id",
              "request.body.model", "model", "raw_model_id", "request.model_route.raw_model_id"],
    "upstream_provider": ["upstream_provider", "provider"],
    "upstream_model": ["upstream_model"],
    "upstream_channel": ["upstream_channel", "channel_id", "channel"],
    "gateway_status": ["gateway_status"],
    "gateway_code": ["gateway_code"],
    "upstream_status": ["upstream_status", "upstream_status_code", "http_status", "provider_status"],
    "error_code": ["upstream_code", "upstream_error_code", "error_code"],
    "error_type": ["upstream_error_type", "error_type", "exception_type"],
    "error_message": ["upstream_message", "upstream_error_message", "exception_message", "error_message"],
    "instance": ["PodName", "__pod_uid__", "__pod_name__", "pod_name", "instance_id", "host", "hostname"],
    "container": ["ContainerName", "__container_name__", "container_name"],
    "logger": ["logger", "source"],
    "event_type": ["event_type", "event"],
    "operation": ["action", "operation", "operation_name"],
}
PREFIX = re.compile(r"^[^\n|]*\|\s*(?P<level>[A-Za-z]+)\s*\|\s*(?P<logger>[A-Za-z_][\w.]*:[A-Za-z_][\w.]*):\d+\s*-\s*")
LEVEL_PREFIX = re.compile(r"^[^\n|]*\|\s*([A-Za-z]+)\s*\|")
SECRET_NAMES = {"authorization", "proxy_authorization", "auth_key", "api_key", "apikey",
                "x_api_key", "token", "access_token", "refresh_token", "client_secret",
                "secret_key", "access_key", "access_key_id", "access_key_secret",
                "password", "secret", "cookie", "set_cookie", "authkey", "xapikey",
                "clientsecret", "accesstoken", "refreshtoken", "secretkey", "accesskey",
                "accesskeyid", "accesskeysecret", "proxyauthorization", "session_token",
                "webhook", "webhook_url", "webhookurl", "license_key", "licensekey",
                "credential", "credentials", "private_key", "privatekey", "signing_key",
                "signingkey", "signing_secret", "signingsecret", "secret_access_key", "secretaccesskey",
                "api_secret", "apisecret", "client_certificate", "clientcertificate",
                "id_token", "idtoken", "jwt"}
BODY_NAMES = {"headers", "request_body", "response_body", "body", "messages", "prompt",
              "request", "response", "input", "output", "content", "payload"}
SOURCE_ID_FIELDS = ("event_id", "EventId", "log_id", "LogId", "row_id", "RowId",
                    "record_id", "RecordId", "_id", "__id__")


def key_name(value):
    return str(value).lower().replace("-", "_")


def json_like(value, depth=0):
    if not isinstance(value, str) or depth > 3 or len(value) > 1000000:
        return value
    for loader in (json.loads, ast.literal_eval):
        try:
            parsed = loader(value)
            return json_like(parsed, depth + 1) if isinstance(parsed, str) and parsed != value else parsed
        except (ValueError, SyntaxError, RecursionError):
            pass
    return value


def bracket_fields(text):
    # Adapted from infra_monitor.request_trace: balanced brackets and quoted JSON.
    fields, spans, cursor = {}, [], 0
    pattern = re.compile(r"\b([A-Za-z_][\w-]*)\s*=\s*\[")
    while match := pattern.search(text, cursor):
        start = end = match.end()
        depth, quote, escaped = 1, "", False
        while end < len(text):
            char = text[end]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif quote:
                if char == quote:
                    quote = ""
            elif char in "\"'" and (not text[start:end].strip() or text[start:end].rstrip()[-1] in "{[:,="):
                quote = char
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        if depth:
            cursor = match.end()
            continue
        key = key_name(match[1])
        fields[key] = text[start:end].strip()
        spans.append((start, end, key))
        cursor = end + 1
    return fields, spans


def lookup(value, path):
    for part in path.split("."):
        value = json_like(value)
        if isinstance(value, list):
            value = {entry.get("name"): entry.get("value") for entry in value
                     if isinstance(entry, dict) and isinstance(entry.get("name"), str) and "value" in entry}
        if not isinstance(value, dict):
            return None
        value = next((child for key, child in value.items() if key_name(key) == key_name(part)), None)
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    return value


def nested_error(value):
    found = {}
    names = {"message": "message", "msg": "message", "detail": "message", "description": "message",
             "reason": "message", "exactdesc": "message", "exact_desc": "message",
             "code": "code", "error_code": "code", "type": "type",
             "status": "status", "status_code": "status", "http_status": "status"}

    def walk(item, depth=0):
        if depth > 12:
            return
        item = json_like(item)
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = key_name(key)
                if normalized in SECRET_NAMES | BODY_NAMES:
                    continue
                parsed = json_like(child)
                if isinstance(parsed, (dict, list)):
                    walk(parsed, depth + 1)
                elif normalized in names and child not in MISSING:
                    found.setdefault(names[normalized], str(child))
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
    walk(value)
    return found


def normalize(row, connection, profile, resource=""):
    warnings = []
    paths = connection.get("content_fields", ["FullLog", "LogMessage", "__content__", "message"])
    original = next((lookup(row, path) for path in paths if lookup(row, path) not in MISSING), row)
    text = original if isinstance(original, str) else json.dumps(original, ensure_ascii=False)
    payload = json_like(text)
    payload = payload if isinstance(payload, dict) else {}
    prefix = PREFIX.match(text)
    source = prefix["logger"] if prefix else str(payload.get("logger", ""))
    message = text[prefix.end():] if prefix else str(payload.get("message", text))
    request_match = re.match(r"Request\s+ID\s*[:=]?\s*(?:\[\s*([^\]]+?)\s*\]|([A-Za-z0-9._:-]+))", message)
    prefix_id = ""
    if request_match and (profile.get("parser") == "gateway" or profile.get("request_id_prefix")):
        prefix_id = request_match[1] or request_match[2]
        message = message[request_match.end():].lstrip()
        if message.startswith("- "):
            message = message[2:].lstrip()
    for rule in profile.get("json_payloads", []):
        if source == rule["logger"] and message.startswith(rule["prefix"]):
            decoded = json_like(message[len(rule["prefix"]):].strip())
            if isinstance(decoded, dict):
                payload = {**payload, rule["target"]: decoded} if rule.get("target") else {**payload, **decoded}
            break
    metadata_text = text
    summary = payload.get("request")
    # A completed gateway record can end in request={...}. Do not promote fields
    # inside that request body into trusted top-level request IDs or status.
    if profile.get("parser") == "gateway" and source == "api.middlewares.ingress.logging:emit_request_completed_log":
        _, spans = bracket_fields(text)
        for match in re.finditer(r"\brequest\s*=\s*(?=\{)", text):
            if any(a <= match.start() < b for a, b, _ in spans):
                continue
            parsed = json_like(text[match.end():].strip())
            if isinstance(parsed, dict):
                summary, metadata_text = parsed, text[:match.start()]
                break
    brackets = bracket_fields(metadata_text)[0] if profile.get("bracket_fields", True) else {}
    metadata = {**brackets, **payload}
    if isinstance(summary, dict):
        metadata["request"] = summary
    sources = [metadata, row]
    result = {}
    for canonical, default_paths in FIELDS.items():
        candidates = profile.get("fields", {}).get(canonical, default_paths)
        if isinstance(candidates, str):
            candidates = [candidates]
        value = next((lookup(item, path) for path in candidates for item in sources
                      if lookup(item, path) not in MISSING and not isinstance(lookup(item, path), (dict, list))), "")
        pattern = profile.get("patterns", {}).get(canonical)
        if not value and pattern:
            match = re.search(pattern, metadata_text)
            if match:
                value = match.groupdict().get("value") or match[1]
        result[canonical] = str(value) if value not in MISSING else ""
    if prefix_id:
        result["request_id"] = prefix_id
    if not result["trace_id"]:
        # W3C traceparent is metadata only, never scanned from an arbitrary body.
        parent = next((lookup(item, path) for item in sources for path in ("traceparent", "headers.traceparent")
                       if lookup(item, path)), "")
        match = re.fullmatch(r"[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}", str(parent), re.I)
        if match and match[1] != "0" * 32:
            result["trace_id"] = match[1].lower()
    level = LEVEL_PREFIX.match(metadata_text)
    if level and (not result["level"] or profile.get("parser") == "gateway" or profile.get("prefer_level_prefix")):
        result["level"] = level[1]
    result["level"] = str(profile.get("level_map", {}).get(result["level"], result["level"]))
    result["level"] = result["level"].strip().upper()
    result["logger"] = source or result["logger"]
    marker = re.match(r"^\[(REQUEST_RECEIVED|REQUEST_COMPLETED|REQUEST_FAILED|VIDEO_CREATE_TIMING)\]", message)
    if marker:
        result["event_type"] = marker[1]
    nested = nested_error(metadata.get("err_msg") or metadata.get("error"))
    for key, value in nested_error(metadata.get("upstream_error")).items():
        nested.setdefault(key, value)
    for target, key in (("error_code", "code"), ("error_type", "type"),
                        ("error_message", "message"), ("upstream_status", "status")):
        result[target] = result[target] or nested.get(key, "")
    if not result["error_message"]:
        for key in ("err_msg", "error", "upstream_error"):
            value = metadata.get(key)
            if value not in MISSING:
                result["error_message"] = str(value)
                break
    result["message"] = message
    stage = "other"
    if profile.get("parser") == "gateway":
        logger = result["logger"]
        event = result["event_type"]
        if "billing" in logger or "worker" in result["container"]:
            stage = "billing"
        elif "emit_request_received_log" in logger or (not logger and event == "REQUEST_RECEIVED"):
            stage = "received"
        elif "emit_request_completed_log" in logger or (not logger and event in {"REQUEST_COMPLETED", "REQUEST_FAILED"}):
            stage = "completed"
        elif "stream_handler" in logger:
            stage = "stream"
        elif "dispatcher" in logger or logger == "api.routes.video_router:_log_video_create_timing" or (not logger and event == "VIDEO_CREATE_TIMING"):
            stage = "upstream"
        elif "auth_key_policy" in logger:
            stage = "auth_observation"
        elif "error_handling" in logger:
            stage = "gateway_error"
    for candidate, pattern in profile.get("stage_patterns", {}).items():
        if re.search(pattern, metadata_text):
            stage = candidate
            break
    result["stage"] = stage
    if profile.get("parser") == "gateway":
        # Gateway policy failures can have no upstream error at all.
        gateway_code = result["gateway_code"]
        if not result["error_code"] and gateway_code not in ("", "0", "OK"):
            result["error_code"] = gateway_code
        if stage == "auth_observation":
            result["error_code"] = result["error_code"] or str(metadata.get("internal_code", ""))
            result["error_message"] = result["error_message"] or str(metadata.get("reason", ""))
    result["outcome"] = "unknown"
    if stage == "completed":
        status = result["gateway_status"]
        if result["event_type"] == "REQUEST_FAILED" or (status.isdigit() and int(status) >= 400) or result["gateway_code"] not in ("", "0", "OK"):
            result["outcome"] = "failed"
        elif status.isdigit() and 200 <= int(status) < 400:
            result["outcome"] = "success"
    stamp = result["timestamp"]
    if stamp:
        try:
            tls_ms = "__time__" in row and str(row["__time__"]) == stamp
            unit = "ms" if tls_ms else profile.get("timestamp_unit")
            result["timestamp"] = parse_time(stamp, profile.get("timezone", "+00:00"), unit).isoformat()
        except (ValueError, OverflowError, TypeError):
            warnings.append("时间字段无法解析，不能据此做时间邻近关联")
            result["timestamp"] = ""
    else:
        warnings.append("未记录时间字段")
    if not result["level"]:
        warnings.append("未记录日志级别，保留该记录")
    source_id = next((lookup(row, path) for path in SOURCE_ID_FIELDS
                      if lookup(row, path) not in MISSING and
                      not isinstance(lookup(row, path), (dict, list))), None)
    identity = ["source_id", str(source_id)] if source_id is not None else ["row", row]
    result.update(event_id=digest([connection["id"], connection["provider"], resource, identity]),
                  connection_id=connection["id"], provider=connection["provider"], resource=resource,
                  service=connection.get("service", ""), environment=connection.get("environment", ""),
                  not_applicable=profile.get("not_applicable", []), raw=row, parse_warnings=warnings,
                  response_label="网关" if profile.get("parser") == "gateway" else "服务")
    return result
