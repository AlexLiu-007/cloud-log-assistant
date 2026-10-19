"""Final-output redaction and channel-neutral notification rendering."""
from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template

from cloud_log_common import parse_time, zone
from cloud_log_parsing import SECRET_NAMES, bracket_fields, json_like, key_name

SCHEME = re.compile(r"^(Bearer|Basic|Token|ApiKey)\s+(.+)$", re.I | re.S)
BEARER = re.compile(r"\b(?:Bearer|Basic|Token|ApiKey)\s+[A-Za-z0-9.*_~+/=-]+", re.I)
SECRET_PATTERN = "|".join(re.escape(name).replace("_", "[-_]?") for name in sorted(SECRET_NAMES, key=len, reverse=True))
LABELED = re.compile(
    r"""(?i)(?<![\w])(?:["']?)(?P<name>""" + SECRET_PATTERN +
    r""")["']?\s*[:=]\s*(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\[[^\]\r\n]*\]|(?:Bearer|Basic|Token|ApiKey)\s+[^\s,;}\]"']+|[^\s,;}\]"']+)""")


def already_masked(value):
    return bool(re.fullmatch(r"(?:[^*]{4})?\*{8}(?:[^*]{4})?", value))


def mask(value):
    value = str(value).strip()
    scheme = SCHEME.match(value)
    if scheme:
        return scheme[1] + " " + mask(scheme[2])
    if not value or value in {"未记录", "不适用", "[已隐藏]"}:
        return value
    if already_masked(value):
        return value
    return "********" if len(value) <= 12 else value[:4] + "********" + value[-4:]


def clean_secret(value):
    value = value.strip()
    if len(value) >= 2 and ((value[0] == value[-1] and value[0] in "\"'") or (value[0] == "[" and value[-1] == "]")):
        value = value[1:-1]
    parsed = json_like(value)
    return parsed if isinstance(parsed, str) else value


def collect_secrets(value):
    found = set()

    def add(item, depth=0):
        if depth > 20:
            return
        if isinstance(item, (list, dict)):
            for child in (item.values() if isinstance(item, dict) else item):
                add(child, depth + 1)
            return
        if isinstance(item, (str, int)) and str(item).strip():
            token = clean_secret(str(item))
            if not already_masked(token):
                found.add(token)
                scheme = SCHEME.match(token)
                if scheme:
                    found.add(scheme[2])

    def walk(item, depth=0):
        if depth > 20:
            return
        if isinstance(item, dict):
            # Some HTTP libraries serialize headers as [{name, value}].
            named = {key_name(key): child for key, child in item.items()}
            if key_name(named.get("name", "")) in SECRET_NAMES and "value" in named:
                add(named["value"])
            for key, child in item.items():
                if key_name(key) in SECRET_NAMES:
                    add(child)
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
        elif isinstance(item, str):
            parsed = json_like(item) if item.lstrip().startswith(("{", "[", '"')) else item
            if isinstance(parsed, (dict, list)):
                walk(parsed, depth + 1)
            fields, _ = bracket_fields(item)
            for key, child in fields.items():
                if key in SECRET_NAMES:
                    add(child)
                else:
                    decoded = json_like(child)
                    if isinstance(decoded, (dict, list)):
                        walk(decoded, depth + 1)
            for match in BEARER.finditer(item):
                add(match[0])
            # Error strings can contain JSON quoted one or more times.
            candidates = [item]
            for _ in range(3):
                decoded = candidates[-1].replace('\\"', '"').replace("\\'", "'")
                if decoded == candidates[-1]:
                    break
                candidates.append(decoded)
            for candidate in candidates:
                for match in LABELED.finditer(candidate):
                    add(match["value"])
    walk(value)
    return found


def redact_text(text, secrets=()):
    text = str(text)
    for secret in sorted(set(secrets), key=len, reverse=True):
        if not secret or already_masked(secret):
            continue
        replacement = mask(secret)
        if len(secret) <= 4:
            text = re.sub(r"(?<![\w])" + re.escape(secret) + r"(?![\w])", lambda _: replacement, text)
        else:
            text = text.replace(secret, replacement)
        escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
        if escaped != secret:
            text = text.replace(escaped, replacement)
    text = BEARER.sub(lambda m: mask(m[0]), text)
    def hide(match):
        value = clean_secret(match["value"])
        return match[0][:match.start("value") - match.start()] + mask(value)
    text = LABELED.sub(hide, text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{9,}", lambda m: mask(m[0]), text)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)


def redact(value, secrets=None):
    secrets = collect_secrets(value) if secrets is None else secrets
    if isinstance(value, dict):
        return {key: mask(child) if key_name(key) in SECRET_NAMES and not isinstance(child, (dict, list))
                else redact(child, secrets) for key, child in value.items()}
    if isinstance(value, list):
        return [redact(child, secrets) for child in value]
    return redact_text(value, secrets) if isinstance(value, str) else value


def public_record(record, extra_secrets=()):
    secrets = collect_secrets(record) | set(extra_secrets)
    return redact({key: value for key, value in record.items() if key != "raw"}, secrets)


def _render_records(document, analyses=None, extra_secrets=()):
    analyses = analyses or {}
    records = document.get("alerts", document.get("records", []))
    default_association = document.get("association", {})
    associations = {event_id: group for group in document.get("associations", [])
                    for event_id in group.get("event_ids", [])}
    secrets = collect_secrets(document) | collect_secrets(analyses) | set(extra_secrets)
    template = Template((Path(__file__).resolve().parents[1] / "assets" / "notification.txt").read_text(encoding="utf-8"))
    timezone = document.get("output_timezone", "+00:00")
    mode_labels = {"identifier": "标识匹配", "business": "业务对象关联", "heuristic": "上下文推测", "context": "无法关联"}
    outcome_labels = {"success": "成功", "failed": "失败", "unknown": "未确认", "stream_error": "流处理异常"}
    sections = []
    for record in records:
        association = associations.get(record["event_id"], default_association)
        excluded = set(record.get("not_applicable", []))
        values = {key: ("不适用" if key in excluded else str(value) if value not in (None, "") else "未记录")
                  for key, value in record.items() if not isinstance(value, (dict, list))}
        for field in ("service", "environment", "provider", "connection_id", "resource", "level", "event_id",
                      "actor", "authorization", "method", "path", "model", "upstream_provider", "upstream_model",
                      "upstream_channel", "gateway_status", "upstream_status", "error_code", "error_type"):
            values.setdefault(field, "不适用" if field in excluded else "未记录")
        values["source_summary"] = record.get("_notification_source_summary") or " / ".join(
            str(record[key]) for key in ("provider", "connection_id", "resource") if record.get(key)) or "未记录"
        values["group_summary_line"] = record.get("_notification_group_summary", "")
        values["timestamp"] = (record.get("_notification_time_label") if record.get("_notification_grouped") else
                               (parse_time(record["timestamp"]).astimezone(zone(timezone)).isoformat()
                                if record.get("timestamp") else "未记录"))
        values["request_identity"] = (record.get("_notification_request_label") if record.get("_notification_grouped") else
                                     (" / ".join(f"{key}={record[key]}" for key in
                                                 ("request_id", "trace_id", "correlation_id") if record.get(key)) or "未记录"))
        if record.get("_notification_grouped"):
            values["event_id"] = record["_notification_event_label"]
        values["authorization"] = mask(values["authorization"])
        values["operation_line"] = "操作类型：" + str(record["operation"]) + "\n" if record.get("operation") else ""
        values["response_label"] = record.get("response_label") or "服务"
        values["error_message"] = record.get("error_message") or ("未记录" if record.get("error_code") else record.get("message")) or "未记录"
        if record.get("_notification_grouped"):
            values["association_mode"] = "多条事件合并展示"
            values["association_evidence"] = "按相同服务、模型、授权 Key 和错误分组；不据此认定为同一请求"
            values["completeness"] = "每条请求链路完整性分别判断；此分组不提供链路证明"
            values["outcome"] = "各事件结果分别保留；分组不推断统一结果"
        elif association:
            values["association_mode"] = mode_labels.get(association.get("mode"), "未确认")
            values["association_evidence"] = "；".join(association.get("evidence", [])) or "证据不足"
            values["completeness"] = ("已观察到入口和结束，限已采集日志" if association.get("completeness") == "observed_boundaries"
                                       else "未确认完整链路")
            values["outcome"] = outcome_labels.get(association.get("outcome"), "未确认")
        else:
            has_id = any(record.get(key) for key in ("request_id", "trace_id", "correlation_id"))
            values.update(association_mode="记录含标识，尚未扩展追踪" if has_id else "无法关联",
                          association_evidence="当前为单条日志事件",
                          completeness="未确认完整链路", outcome="未确认")
        analysis = analyses.get(record["event_id"], {})
        certainty = {"confirmed": "", "suspected": "可能：", "unknown": "待确认："}.get(analysis.get("certainty"), "待确认：")
        values["judgment"] = certainty + analysis["judgment"] if analysis.get("judgment") else "暂无法确认原因"
        values["analysis_evidence"] = analysis.get("evidence") or ("事件 " + record["event_id"])
        values["recommendation"] = analysis.get("recommendation") or "补查请求上下文，核对最终响应"
        sections.append(template.substitute(values).rstrip())
    diagnostics = []
    for source in document.get("sources", []):
        label = " / ".join(str(source[key]) for key in ("provider", "connection_id", "resource") if source.get(key))
        if source.get("error"):
            diagnostics.append(label + "：查询失败，" + source["error"])
        elif source.get("status") == "failed":
            diagnostics.append(label + "：查询失败，未获得可靠结果")
        elif source.get("complete") is False:
            diagnostics.append(label + "：查询结果不完整")
        diagnostics.extend(label + "：" + warning for warning in source.get("warnings", []))
    if document.get("complete") is False and not diagnostics:
        diagnostics.append("查询结果不完整，不能据此确认没有其他异常")
    diagnostics.extend(default_association.get("warnings", []))
    for group in document.get("associations", []):
        diagnostics.extend(group.get("warnings", []))
    if diagnostics:
        sections.append("【查询与追踪说明】\n" + "\n".join(dict.fromkeys(diagnostics)))
    if not sections:
        sections.append("本次查询未获得符合条件的日志；范围及完整性以查询结果为准。")
    return redact_text("\n\n---\n\n".join(sections) + "\n", secrets)


def _group_text(value):
    normalized = str(value).strip() if value not in (None, "") else ""
    return "" if normalized.casefold() in {"未记录", "不适用", "unknown", "none", "null", "n/a", "-"} else normalized


def _group_error_text(value):
    return " ".join(_group_text(value).split()).casefold()


def _group_auth(value, fingerprints, event_id):
    fingerprint = fingerprints.get(event_id)
    if fingerprint:
        return "state-hmac", fingerprint
    if not isinstance(value, (str, int)):
        return None
    value = str(value).strip()
    value = re.sub(r"^(?:Bearer|Basic|Token|ApiKey)\s+", "", value, count=1, flags=re.I).strip()
    if (not value or value.casefold() in {"未记录", "不适用", "[已隐藏]"} or
            re.fullmatch(r"(?:[^*]{4})?\*{8}(?:[^*]{4})?", value)):
        return None
    return "raw", value


def _group_key(record, association, analysis, fingerprints):
    model = _group_text(record.get("model"))
    authorization = _group_auth(record.get("authorization"), fingerprints, record.get("event_id"))
    error = (_group_error_text(record.get("error_code")), _group_error_text(record.get("error_type")),
             _group_error_text(record.get("error_message") or record.get("message")))
    if not model or not authorization or not any(error):
        return None
    association, analysis = association or {}, analysis or {}
    return (
        _group_text(record.get("provider")), _group_text(record.get("service")),
        _group_text(record.get("environment")), model, authorization, error,
        _group_text(record.get("stage")), _group_text(record.get("event_type")),
        _group_text(association.get("outcome", record.get("outcome", ""))),
        _group_text(association.get("completeness")), _group_text(record.get("gateway_status")),
        _group_text(record.get("upstream_status")), _group_text(record.get("response_label")),
        _group_text(analysis.get("certainty")), _group_text(analysis.get("judgment")),
        _group_text(analysis.get("recommendation")),
    )


def _distinct(records, field):
    return list(dict.fromkeys(str(record[field]) for record in records
                              if record.get(field) not in (None, "")))


def _time_range(records, timezone):
    times = []
    for record in records:
        if record.get("timestamp"):
            try:
                times.append(parse_time(record["timestamp"]).astimezone(zone(timezone)))
            except Exception:
                continue
    if not times:
        return "未记录"
    times.sort()
    first, last = times[0].isoformat(), times[-1].isoformat()
    return first if first == last else first + " 至 " + last


def render(document, analyses=None, extra_secrets=(), group_fingerprints=None):
    """Merge equivalent alert presentation while leaving source events untouched."""
    analyses = analyses or {}
    group_fingerprints = group_fingerprints or {}
    field = "alerts" if "alerts" in document else "records"
    records = document.get(field, [])
    if not records:
        return _render_records(document, analyses, extra_secrets)
    associations = {event_id: group for group in document.get("associations", [])
                    for event_id in group.get("event_ids", [])}
    default_association = document.get("association", {})
    groups, positions = [], {}
    for record in records:
        association = associations.get(record.get("event_id"), default_association)
        analysis = analyses.get(record.get("event_id"), {})
        key = _group_key(record, association, analysis, group_fingerprints)
        if key is None:
            groups.append([record])
        elif key in positions:
            groups[positions[key]].append(record)
        else:
            positions[key] = len(groups)
            groups.append([record])

    if not any(len(group) > 1 for group in groups):
        return _render_records(document, analyses, extra_secrets)

    extra_secrets = set(extra_secrets) | collect_secrets(document) | collect_secrets(analyses)
    rendered_records, grouped_associations = [], []
    timezone = document.get("output_timezone", "+00:00")
    for group in groups:
        record = dict(group[0])
        if len(group) > 1:
            count = len(group)
            source_labels = list(dict.fromkeys(
                " / ".join(str(item[key]) for key in ("provider", "connection_id", "resource") if item.get(key))
                for item in group))
            record.update(
                _notification_grouped=True,
                _notification_group_summary=(f"合并展示：{count} 条独立事件；时间范围：{_time_range(group, timezone)}；"
                                             f"涉及来源：{len(source_labels)} 个\n"),
                _notification_source_summary="；".join(source_labels) or "未记录",
                _notification_time_label=f"多次（{count} 条事件，见合并摘要）",
                _notification_request_label=f"{count} 条事件的请求标识分别保留；合并不代表同一请求",
                _notification_event_label=f"{count} 条独立事件（各 event_id 与回执逐条保留）",
            )
            for name in ("level", "actor", "method", "path", "operation", "upstream_provider",
                         "upstream_model", "upstream_channel"):
                values = _distinct(group, name)
                if len(values) > 1:
                    record[name] = "多个：" + "；".join(values)
            grouped_associations.append({
                "event_ids": [record["event_id"]], "mode": "context", "evidence": [],
                "completeness": "not_confirmed", "outcome": "unknown",
            })
        rendered_records.append(record)

    rendered_document = dict(document)
    rendered_document[field] = rendered_records
    rendered_document["associations"] = list(document.get("associations", [])) + grouped_associations
    return _render_records(rendered_document, analyses, extra_secrets)
