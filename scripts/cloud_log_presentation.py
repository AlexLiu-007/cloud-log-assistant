"""Compact notifications with deterministic final redaction."""
from __future__ import annotations

from pathlib import Path
from string import Template

from cloud_log_common import parse_time, zone
from cloud_log_grouping import notification_groups, request_identity, value
from cloud_log_reporting import collect_secrets, mask, redact_text


def _unique(values):
    return list(dict.fromkeys(item for item in values if item))


def _clip(text, limit, enabled):
    text = " ".join(str(text).split())
    return text[:limit] + "…（完整内容见详情）" if enabled and len(text) > limit else text


def _time_range(records, timezone):
    times = []
    for record in records:
        try:
            if record.get("timestamp"):
                times.append(parse_time(record["timestamp"]).astimezone(zone(timezone)))
        except (ValueError, TypeError, OverflowError):
            continue
    if not times:
        return "时间未记录"
    times.sort()
    first, last = times[0].isoformat(), times[-1].isoformat()
    return first if first == last else first + " 至 " + last


def diagnostics(document):
    messages = []
    for source in document.get("sources", []):
        label = " / ".join(str(source[key]) for key in ("provider", "connection_id", "resource") if source.get(key))
        if source.get("error") or source.get("status") == "failed":
            messages.append(label + "：查询失败，" + str(source.get("error") or "未获得可靠结果"))
        elif source.get("complete") is False:
            messages.append(label + "：查询结果不完整")
        messages.extend(label + "：" + str(warning) for warning in source.get("warnings", []))
    if document.get("complete") is False and not messages:
        messages.append("查询结果不完整，不能据此确认没有其他异常")
    for association in [document.get("association", {}), *document.get("associations", [])]:
        messages.extend(str(warning) for warning in association.get("warnings", []))
    return _unique(messages)


def render(document, analyses=None, extra_secrets=(), fingerprints=None, *, allow_truncation=False):
    groups = notification_groups(document, analyses, fingerprints)
    secrets = collect_secrets(document) | collect_secrets(analyses or {}) | set(extra_secrets)
    # Redact before truncation; a cut credential must not escape exact-value matching.
    clean = lambda text: redact_text(str(text), secrets)
    has_report = allow_truncation and bool(document.get("notification_report_id"))
    template = Template((Path(__file__).resolve().parents[1] / "assets/notification.txt").read_text(encoding="utf-8"))
    sections = []
    for group in groups:
        rows, first = group["records"], group["records"][0]
        identities = {request_identity(row) for row in rows if request_identity(row) is not None}
        unknown = sum(request_identity(row) is None for row in rows)
        counts = f"{len(rows)} 条异常日志"
        if identities:
            counts += f" · {len(identities)} 个已识别请求"
        if unknown:
            counts += f" · {unknown} 条缺少请求标识"
        details = [counts + " · " + _time_range(rows, document.get("output_timezone", "+00:00"))]
        levels = _unique(str(row.get("level", "")) for row in rows)
        if levels:
            details.append("日志级别：" + " / ".join(levels))
        if value(first.get("model")):
            details.append("模型：" + clean(first["model"]))
        if value(first.get("authorization")):
            details.append("Key：" + mask(first["authorization"]))
        status = " / ".join(label + " " + value(first.get(field)) for label, field in (
            (first.get("response_label") or "服务", "gateway_status"), ("上游", "upstream_status")) if value(first.get(field)))
        if status:
            details.append("响应状态：" + clean(status))
        sources = _unique(" / ".join(str(row[key]) for key in ("connection_id", "resource") if row.get(key)) for row in rows)
        if sources:
            details.append("来源：" + _clip(clean("；".join(sources)), 200, has_report))
        upstream = " / ".join(value(first.get(key)) for key in ("upstream_provider", "upstream_channel", "upstream_model") if value(first.get(key)))
        if upstream:
            details.append("上游：" + clean(upstream))
        if value(first.get("operation")):
            details.append("操作类型：" + clean(first["operation"]))
        errors = _unique(value(row.get("error_message")) for row in rows)
        codes = _unique(value(row.get("error_code")) for row in rows)
        types = _unique(value(row.get("error_type")) for row in rows)
        error_lines = []
        if codes or types:
            error_lines.append("错误代码/类型：" + clean(" / ".join(codes + types)))
        classifications = list(group["classifications"].values())
        if classifications:
            error_lines.append("错误摘要：" + _clip(clean(classifications[0]["summary"]), 160, has_report))
        shown = errors[:1] if has_report else errors
        error_lines.append("错误信息：" + ("；".join(_clip(clean(item), 240, has_report) for item in shown) or "未记录"))
        if has_report and len(errors) > 1:
            error_lines.append(f"原文示例 1/{len(errors)}；全部变体见详情")
        evidence_types = _unique(str(item.get("event_type") or item.get("stage") or "事件")
            for association in group["associations"].values() for item in association.get("error_evidence", [])
            if item.get("event_id") in {event_id for field in ("error_code", "error_type", "error_message")
                                       for event_id in association.get("field_sources", {}).get(field, [])})
        if evidence_types:
            error_lines.append("错误来源：关联日志：" + clean("、".join(evidence_types)))
        judgments, recommendations = [], []
        for analysis in group["analyses"].values():
            if analysis.get("judgment"):
                prefix = {"confirmed": "", "suspected": "可能："}.get(analysis.get("certainty"), "待确认：")
                judgments.append(prefix + str(analysis["judgment"]))
            if analysis.get("recommendation"):
                recommendations.append(str(analysis["recommendation"]))
        analysis_lines = []
        for label, texts in (("分析", _unique(judgments)), ("建议", _unique(recommendations))):
            if texts:
                displayed = texts[:1] if has_report else texts
                analysis_lines.append(label + "：" + "；".join(_clip(clean(item), 160, has_report) for item in displayed)
                                      + (f"（另有 {len(texts)-1} 项见详情）" if has_report and len(texts) > 1 else ""))
        title = "日志告警｜" + "｜".join(clean(first[key]) for key in ("service", "environment") if value(first.get(key)))
        sections.append(template.substitute(title=title.rstrip("｜"), details="\n".join(details),
                                            error="\n".join(error_lines), analysis="\n".join(analysis_lines)).strip())
    notes = diagnostics(document)
    if notes:
        sections.append("【查询与追踪说明】\n" + "\n".join(_clip(clean(item), 240, has_report) for item in notes))
    if not sections:
        sections.append("本次查询未获得符合条件的日志；范围及完整性以查询结果为准。")
    if has_report:
        sections.append("详情编号：" + clean(document["notification_report_id"]) + "（可交给 Agent 查询）")
    return clean("\n\n---\n\n".join(sections) + "\n")
