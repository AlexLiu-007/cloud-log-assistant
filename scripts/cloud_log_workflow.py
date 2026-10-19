"""Local follow-up planning. The host agent executes queries and supplies analysis."""
from __future__ import annotations

from datetime import timedelta

from cloud_log_common import LogError, digest, parse_time

REQUEST_IDS = ("request_id", "trace_id", "correlation_id")
ERROR_FIELDS = ("error_code", "error_type", "error_message", "upstream_status")
TRACE_STATES = {"complete", "partial", "failed"}


def present(value):
    return value not in (None, "", "-", "None", "null", "未记录", "不适用")


def has_error(record):
    return any(present(record.get(key)) for key in ERROR_FIELDS[:3])


def failure_reasons(record):
    reasons = []
    failed_status = any(str(record.get(key, "")).isdigit() and int(record[key]) >= 400
                        for key in ("gateway_status", "upstream_status"))
    if failed_status or record.get("outcome") in {"failed", "stream_error"} or record.get("event_type") == "REQUEST_FAILED":
        reasons.append("failure_observed")
    if has_error(record) and record.get("stage") != "completed":
        reasons.append("error_needs_final_outcome")
    if str(record.get("level", "")).upper() in {"WARN", "WARNING", "ERROR", "CRITICAL", "FATAL"} and record.get("outcome") != "success":
        reasons.append("abnormal_event")
    return reasons


def scoped_sources(document, record):
    return [source for source in document.get("sources", []) if
            (source.get("connection_id"), source.get("resource", "")) ==
            (record.get("connection_id"), record.get("resource", ""))]


def query_status(document, sources):
    """Query coverage is separate from observing request start/end boundaries."""
    if sources and all(source.get("status") == "failed" for source in sources):
        return "failed"
    complete = (all(source.get("complete") is True and source.get("status") != "failed" for source in sources)
                if sources else document.get("complete") is True)
    if document.get("selection") == "exclude_INFO_DEBUG":
        complete = False
    return "complete" if complete else "partial"


def covers_anchor(document, sources, record):
    """Reject an unrelated time range even when an identifier was reused."""
    if not record.get("timestamp"):
        return True
    try:
        timestamp = parse_time(record["timestamp"])
        ranges = sources or [document]
        for source in ranges:
            start, end = source.get("query_start"), source.get("query_end")
            if start and end and not parse_time(start) <= timestamp < parse_time(end):
                return False
    except (ValueError, TypeError, OverflowError, LogError):
        return False
    return True


def plan_investigation(document, *, window_seconds=None, trace_all=None):
    previous = document.get("investigation", {})
    seconds = window_seconds if window_seconds is not None else previous.get("trace_window_seconds", 300)
    if type(seconds) is not int or not 1 <= seconds <= 86400:
        raise LogError("trace_window_seconds 需要 1 到 86400 之间的整数")
    force = bool(previous.get("trace_all", False) if trace_all is None else trace_all)
    groups = {event_id: group for group in document.get("associations", []) for event_id in group.get("event_ids", [])}
    states, tasks = [], []
    for record in document.get("alerts", document.get("records", [])):
        association = groups.get(record["event_id"], document.get("association", {}))
        reasons = failure_reasons(record)
        if force:
            reasons.append("user_requested_trace")
        field = next((key for key in REQUEST_IDS if present(record.get(key)) and "*" not in str(record[key])), None)
        status = association.get("trace_status")
        if association.get("trace_attempted") is False:
            status = None
        elif status not in TRACE_STATES:
            status = "complete" if association.get("completeness") == "observed_boundaries" else None
        chain = "ambiguous" if association.get("ambiguous") else association.get("completeness", "unconfirmed")
        state = {"event_id": record["event_id"], "reasons": reasons,
                 "trace_status": status or ("not_started" if reasons else "not_required"),
                 "chain_status": chain,
                 "error_evidence_status": "conflicting" if association.get("error_conflict") else
                     "recorded" if has_error(record) else "context_available" if association.get("error_evidence") else
                     ("not_found_in_scope" if status == "complete" else "unverified")}
        if state["trace_status"] == "not_started":
            missing = []
            if not field:
                missing.append("request_identifier")
            if not record.get("connection_id"):
                missing.append("connection_id")
            if not record.get("resource") or record.get("resource") == "offline":
                missing.append("resource")
            try:
                anchor = parse_time(record["timestamp"])
                start, end = anchor - timedelta(seconds=seconds), anchor + timedelta(seconds=seconds)
            except (KeyError, ValueError, TypeError, OverflowError, LogError):
                start = end = None
                missing.append("timestamp")
            if missing:
                state.update(trace_status="blocked", missing=missing, next_step="report_missing_scope")
            else:
                scope = (record["connection_id"], record["resource"], field, record[field])
                task = next((task for task in tasks if task["scope"] == scope and
                             start <= task["end"] and end >= task["start"]), None)
                if task is None:
                    task = {"scope": scope, "start": start, "end": end, "states": []}
                    tasks.append(task)
                task["start"], task["end"] = min(task["start"], start), max(task["end"], end)
                task["states"].append(state)
        elif state["trace_status"] in TRACE_STATES:
            state["next_step"] = ("analyze_evidence" if status == "complete" and chain == "observed_boundaries"
                                  else "analyze_with_limitations")
        states.append(state)

    actions = []
    for task in tasks:
        connection, resource, field, value = task["scope"]
        parameters = {"connection": connection, "resource": resource, "id_field": field, "id_value": value,
                      "start": task["start"].isoformat(), "end": task["end"].isoformat()}
        task_id = digest(parameters)[:20]
        for state in task["states"]:
            state.update(task_id=task_id, next_step="execute_trace_then_enrich")
        actions.append({"task_id": task_id, "action": "trace",
                        "event_ids": [state["event_id"] for state in task["states"]],
                        "parameters": parameters,
                        "argv": ["trace", *("--" + key.replace("_", "-") + "=" + str(value)
                                             for key, value in parameters.items())],
                        "include_all_levels": True, "on_result": "enrich_even_if_empty_or_failed"})
    limited = any(state["trace_status"] in {"blocked", "partial", "failed"} or state["error_evidence_status"] == "conflicting" or
                  (state["reasons"] and (state["chain_status"] != "observed_boundaries" or
                                          state["error_evidence_status"] in {"unverified", "not_found_in_scope"}))
                  for state in states)
    return {"schema_version": 1, "trace_window_seconds": seconds, "trace_all": force,
            "status": "needs_trace" if actions else "limited" if limited else "ready",
            "pending_trace_count": len(actions), "event_count": len(states),
            "next_actions": actions, "events": states}
