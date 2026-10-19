"""Compact notifications with deterministic final redaction."""
from __future__ import annotations

from pathlib import Path
from string import Template

from cloud_log_common import parse_time, zone
from cloud_log_grouping import notification_groups, notification_sections, value
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


def _error_block(group, index, clean, has_report):
    rows = group["records"]
    errors = _unique(value(row.get("error_message")) for row in rows)
    codes = _unique(value(row.get("error_code")) for row in rows)
    types = _unique(value(row.get("error_type")) for row in rows)
    lines = [f"【错误 {index}】" + (" " + clean(" / ".join(_unique(codes + types))) if codes or types else "")]
    summaries = _unique(item["summary"] for item in group["classifications"].values())
    if len(summaries) == 1:
        lines.append("错误摘要：" + _clip(clean(summaries[0]), 160, has_report))
    shown = errors[:1] if has_report else errors
    lines.append("错误信息：" + ("；".join(_clip(clean(item), 240, has_report) for item in shown) or "未记录"))
    if has_report and len(errors) > 1:
        lines.append(f"原文示例 1/{len(errors)}；全部变体见详情")
    analysis = group["analysis"]
    if group["analysis_issue"]:
        lines.append("分析：" + group["analysis_issue"])
    elif analysis:
        prefix = {"confirmed": "", "suspected": "可能："}.get(analysis.get("certainty"), "待确认：")
        lines.append("分析：" + prefix + _clip(clean(analysis["judgment"]), 160, has_report))
        if analysis["recommendation"]:
            lines.append("建议：" + _clip(clean(analysis["recommendation"]), 160, has_report))
    else:
        lines.append("分析：暂无法确认原因，需 Agent 根据错误证据分析")
    # List distinct identifier values, not inferred request counts. Do not shorten
    # IDs even with a report: delivery splits oversized cards without losing IDs.
    request_ids = _unique(value(row.get("request_id")) for row in rows)
    missing = sum(not value(row.get("request_id")) for row in rows)
    if request_ids:
        lines.append(f"request_id（{len(request_ids)} 个）：")
        lines.extend(clean(request_id) for request_id in request_ids)
    if missing:
        lines.append(f"{missing} 条日志未记录 request_id")
    return "\n".join(lines)


def render(document, analyses=None, extra_secrets=(), fingerprints=None, *, allow_truncation=False,
           prepared_groups=None):
    # The delivery layer may select a subset for one target. Keep the diagnosis
    # resolved against the full batch rather than picking that target's member.
    groups = notification_groups(document, analyses, fingerprints) if prepared_groups is None else prepared_groups
    secrets = collect_secrets(document) | collect_secrets(analyses or {}) | set(extra_secrets)
    # Redact before truncation; a cut credential must not escape exact-value matching.
    clean = lambda text: redact_text(str(text), secrets)
    has_report = allow_truncation and bool(document.get("notification_report_id"))
    template = Template((Path(__file__).resolve().parents[1] / "assets/notification.txt").read_text(encoding="utf-8"))
    sections = []
    for section in notification_sections(groups):
        rows, first = section["records"], section["records"][0]
        counts = f"{len(rows)} 条异常日志 · {len(section['groups'])} 类错误"
        details = [counts + " · " + _time_range(rows, document.get("output_timezone", "+00:00"))]
        levels = _unique(str(row.get("level", "")) for row in rows)
        if levels:
            details.append("日志级别：" + " / ".join(levels))
        services = _unique(" / ".join(value(row.get(key)) for key in ("service", "environment") if value(row.get(key))) for row in rows)
        if services:
            details.append("服务：" + _clip(clean("；".join(services)), 200, has_report))
        sources = _unique(" / ".join(str(row[key]) for key in ("connection_id", "resource") if row.get(key)) for row in rows)
        if sources:
            details.append("来源：" + _clip(clean("；".join(sources)), 200, has_report))
        title = "日志告警｜Key：" + (clean(mask(first["authorization"])) if value(first.get("authorization")) else "未记录")
        if value(first.get("model")):
            title += "｜模型：" + clean(first["model"])
        blocks = [_error_block(group, index, clean, has_report)
                  for index, group in enumerate(section["groups"], 1)]
        sections.append(template.substitute(title=title, details="\n".join(details), errors="\n\n".join(blocks)).strip())
    notes = diagnostics(document)
    if notes:
        sections.append("【查询与追踪说明】\n" + "\n".join(_clip(clean(item), 240, has_report) for item in notes))
    if not sections:
        sections.append("本次查询未获得符合条件的日志；范围及完整性以查询结果为准。")
    if has_report:
        sections.append("详情编号：" + clean(document["notification_report_id"]) + "（可交给 Agent 查询）")
    return clean("\n\n---\n\n".join(sections) + "\n")
