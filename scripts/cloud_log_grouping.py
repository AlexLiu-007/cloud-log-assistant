"""Validate agent classifications and group notification presentation, not requests."""
from __future__ import annotations

import re

from cloud_log_common import LogError, digest

CATEGORIES = {
    "content_policy", "invalid_request", "authentication", "permission", "rate_limit",
    "quota", "not_found", "timeout", "unavailable", "internal",
}
LEVELS = {"DEBUG": 0, "INFO": 1, "NOTICE": 2, "WARN": 3, "WARNING": 3,
          "ERROR": 4, "CRITICAL": 5, "FATAL": 6}
ERROR_FIELDS = ("error_code", "error_type", "error_message")


def value(item):
    text = str(item).strip() if item is not None else ""
    return "" if text.casefold() in {"", "未记录", "不适用", "unknown", "none", "null", "n/a", "-"} else text


def validate_classification(record, association, analysis):
    """Check evidence membership and shape. Semantic correctness remains the agent's job."""
    proposal = analysis.get("classification")
    if proposal is None:
        return None, ""
    if not isinstance(proposal, dict) or analysis.get("certainty") != "confirmed":
        return None, "分类需要已确认的分析和结构化证据"
    if association.get("ambiguous") or association.get("error_conflict"):
        return None, "关联证据存在歧义或错误冲突"
    category, reason, subject = (proposal.get(key) for key in ("category", "reason", "subject"))
    if not isinstance(category, str) or category not in CATEGORIES:
        return None, "分类不在约定列表中"
    if (not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", reason)
            or not isinstance(subject, str) or not subject.strip() or len(subject) > 128):
        return None, "分类需要稳定的具体原因和受影响对象"
    summary, quote = proposal.get("summary"), proposal.get("evidence_quote")
    ids = proposal.get("evidence_event_ids")
    if (not isinstance(summary, str) or not summary.strip() or len(summary) > 240
            or not isinstance(quote, str) or not 4 <= len(quote) <= 2000
            or not isinstance(ids, list) or not 1 <= len(ids) <= 20
            or any(not isinstance(event_id, str) for event_id in ids)):
        return None, "分类摘要或证据格式无效"
    source_ids = {event_id for field in ERROR_FIELDS
                  for event_id in association.get("field_sources", {}).get(field, [])}
    candidates = [record, *(item for item in association.get("error_evidence", [])
                            if item.get("event_id") in source_ids)]
    evidence = {item.get("event_id"): item for item in candidates if isinstance(item, dict)
                and all(item.get(key, "") == record.get(key, "") for key in ("connection_id", "resource"))
                and any(value(item.get(key)) for key in ERROR_FIELDS)}
    if not set(ids) <= evidence.keys() or not any(
            quote in value(evidence[event_id].get(key)) for event_id in ids for key in ERROR_FIELDS):
        return None, "分类引用未匹配当前事件或其关联错误字段"
    # Comparing source error codes/types in the group key prevents a broad category
    # from overriding an explicit conflicting error code.
    return {**proposal, "subject": subject.strip(), "summary": summary.strip()}, ""


def authorization_identity(record, fingerprints):
    fingerprint = fingerprints.get(record["event_id"])
    if fingerprint:
        return ("hmac", fingerprint)
    authorization = value(record.get("authorization"))
    authorization = re.sub(r"^(?:Bearer|Basic|Token|ApiKey)\s+", "", authorization, flags=re.I)
    if not authorization or "*" in authorization or authorization == "[已隐藏]":
        return None
    return ("raw", authorization)


def shared_analysis(group):
    """Only publish a diagnosis the Agent supplied consistently for every member."""
    proposals = []
    for record in group["records"]:
        analysis = group["analyses"][record["event_id"]]
        certainty = analysis.get("certainty")
        if certainty not in ("confirmed", "suspected", "unknown"):
            certainty = "unknown"
        proposals.append((value(analysis.get("judgment")), value(analysis.get("recommendation")), certainty))
    if not any(judgment or recommendation for judgment, recommendation, _ in proposals):
        return {}, ""
    if any(not judgment for judgment, _, _ in proposals):
        return {}, "该错误组缺少覆盖全部事件的统一分析，请 Agent 补全"
    if len(set(proposals)) != 1:
        return {}, "同组分析或建议不一致，请 Agent 结合全部证据统一分析"
    judgment, recommendation, certainty = proposals[0]
    return {"judgment": judgment, "recommendation": recommendation, "certainty": certainty}, ""


def notification_groups(document, analyses=None, fingerprints=None):
    saved = document.get("notification_analyses", {})
    if not isinstance(saved, dict) or (analyses is not None and not isinstance(analyses, dict)):
        raise LogError("analysis-file 需要以 event_id 为键的分析对象")
    analyses = {**saved, **(analyses or {})}
    if any(not isinstance(item, dict) for item in analyses.values()):
        raise LogError("analysis-file 需要以 event_id 为键的分析对象")
    associations = {event_id: item for item in document.get("associations", [])
                    for event_id in item.get("event_ids", [])}
    groups, positions, seen = [], {}, set()
    for record in document.get("alerts", document.get("records", [])):
        event_id = record.get("event_id")
        if not isinstance(event_id, str) or not event_id or event_id in seen:
            raise LogError("通知事件需要唯一的 event_id")
        seen.add(event_id)
        association = associations.get(event_id, document.get("association", {}))
        analysis = analyses.get(event_id, {})
        classification, issue = validate_classification(record, association, analysis)
        authorization = authorization_identity(record, fingerprints or {})
        errors = tuple(value(record.get(key)) for key in ERROR_FIELDS)
        # Presentation is Key/model -> error. Scope remains on the records for
        # tracing and receipts, but status, source and lifecycle never split cards.
        primary = (("key-model-v2", authorization, value(record.get("model")))
                   if authorization else ("unidentified-key-v2", event_id))
        signature = None
        if authorization and any(errors):
            error_identity = (("agent", classification["category"], classification["reason"], classification["subject"],
                               errors[0], errors[1]) if classification else ("exact", *errors))
            signature = (primary, error_identity)
        if signature is None or signature not in positions:
            group = {"id": digest(signature) if signature is not None else digest(["event", event_id]),
                     "primary_id": digest(primary),
                     "mergeable": signature is not None, "records": [], "analyses": {},
                     "classifications": {}, "associations": {}, "classification_issues": {}}
            if signature is not None:
                positions[signature] = len(groups)
            groups.append(group)
        else:
            group = groups[positions[signature]]
        group["records"].append(record)
        group["analyses"][event_id] = analysis
        group["associations"][event_id] = association
        if classification:
            group["classifications"][event_id] = classification
        if issue:
            group["classification_issues"][event_id] = issue
    for group in groups:
        group["level"] = max(LEVELS.get(str(row.get("level", "")).upper(), 2) for row in group["records"])
        group["analysis"], group["analysis_issue"] = shared_analysis(group)
    return sorted(groups, key=lambda group: -group["level"])


def notification_sections(groups):
    """Nest error groups without changing their individual cooldown/receipt IDs."""
    sections = {}
    for group in groups:
        section = sections.setdefault(group["primary_id"], {"records": [], "groups": []})
        section["records"].extend(group["records"])
        section["groups"].append(group)
    return list(sections.values())
