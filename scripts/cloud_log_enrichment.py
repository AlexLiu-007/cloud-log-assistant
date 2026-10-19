"""Fill missing alert fields from explicit identifiers without changing a delivery batch."""
from __future__ import annotations

from copy import deepcopy

from cloud_log_common import LogError, parse_time
from cloud_log_correlation import correlate
from cloud_log_reporting import collect_secrets, public_record
from cloud_log_workflow import (ERROR_FIELDS, covers_anchor, has_error, plan_investigation,
                                query_status, scoped_sources)

IDENTIFIERS = ("request_id", "trace_id", "correlation_id")
METADATA = ("actor", "authorization", "method", "path", "model", "upstream_provider",
            "upstream_channel", "upstream_model", "operation")
CONTEXT_FIELDS = set(IDENTIFIERS + METADATA + ERROR_FIELDS + (
    "event_id", "connection_id", "resource", "provider", "service", "environment", "timestamp",
    "stage", "outcome", "level", "event_type", "instance", "business_id", "gateway_status", "gateway_code"))


def context_record(record, secrets):
    return public_record({key: value for key, value in record.items() if key in CONTEXT_FIELDS}, secrets)


def merge_observation(observations, current):
    """A successful query covering an earlier interval supersedes its failure."""
    def scope(item):
        return [(source.get("connection_id"), source.get("resource", ""),
                 source.get("query_start"), source.get("query_end")) for source in item["sources"]]
    if current["status"] == "complete" and current.get("all_levels", True):
        def covered(old):
            if scope(old) == scope(current):
                return True
            try:
                return bool(old["sources"]) and all(any(
                    (source.get("connection_id"), source.get("resource", "")) ==
                    (new.get("connection_id"), new.get("resource", "")) and
                    parse_time(new["query_start"]) <= parse_time(source["query_start"]) and
                    parse_time(new["query_end"]) >= parse_time(source["query_end"])
                    for new in current["sources"]) for source in old["sources"])
            except (KeyError, ValueError, TypeError, OverflowError, LogError):
                return False
        observations = [old for old in observations if not covered(old)]
    if current not in observations:
        observations.append(current)
    return observations


def enrich(document, contexts):
    result = deepcopy(document)
    key = "alerts" if "alerts" in result else "records"
    if not isinstance(result.get(key), list):
        raise LogError("enrich 输入需要 alerts 或 records 数组")
    associations, enriched = [], []
    previous = {event_id: group for group in document.get("associations", []) for event_id in group.get("event_ids", [])}
    secrets = collect_secrets(document) | collect_secrets(contexts)
    for original in result[key]:
        alert = deepcopy(original)
        field = next((name for name in IDENTIFIERS if alert.get(name)), None)
        old = previous.get(alert["event_id"], {})
        # Recompute previously borrowed values, so a later conflicting context cannot
        # leave a stale, apparently confirmed error in the alert.
        for name in old.get("field_sources", {}):
            alert[name] = ""
        candidates = {r["event_id"]: r for r in old.get("context_records", [])}
        candidates[alert["event_id"]] = alert
        consulted = []
        observations = list(old.get("trace_queries", []))
        if field:
            for context in contexts:
                sources = scoped_sources(context, alert)
                if context.get("sources") and not sources:
                    continue
                if not covers_anchor(context, sources, alert):
                    continue
                matching = [r for r in context.get("records", context.get("alerts", []))
                            if (r.get("connection_id"), r.get("resource", "")) ==
                               (alert["connection_id"], alert.get("resource", "")) and r.get(field) == alert[field]]
                intent = context.get("matched_identifier", {})
                attempted = (bool(sources) and context.get("kind") == "trace" and
                             intent.get("field") == field and intent.get("value") == alert[field])
                if matching or attempted:
                    candidates.update((r["event_id"], context_record(r, secrets) if key == "alerts" else r)
                                      for r in matching)
                    status = query_status(context, sources)
                    consulted.append(status)
                    observation = {"status": status, "matched_records": len(matching),
                                   "all_levels": context.get("selection") != "exclude_INFO_DEBUG",
                                   "sources": [{key: source[key] for key in
                                                ("connection_id", "resource", "query_start", "query_end", "status",
                                                 "complete", "warnings", "error") if key in source} for source in sources]}
                    observations = merge_observation(observations, observation)
        if field and consulted:
            statuses = [observation["status"] for observation in observations if observation.get("all_levels", True)]
            status = ("not_started" if not statuses else
                      "failed" if all(value == "failed" for value in statuses) else
                      "complete" if all(value == "complete" for value in statuses) else "partial")
            group = correlate(list(candidates.values()), alert["event_id"], field=field, complete=status == "complete")
            group.update(trace_status=status, trace_attempted=bool(statuses), trace_queries=observations)
            if not any(observation["matched_records"] for observation in observations):
                group["warnings"].append("已尝试按标识补查，但未获得匹配记录；这不代表请求没有错误")
            for observation in observations:
                for source in observation["sources"]:
                    if source.get("error"):
                        group["warnings"].append("追踪查询失败：" + source["error"])
                    group["warnings"].extend(source.get("warnings", []))
            provenance = {}
            if not group["ambiguous"]:
                fields = list(METADATA)
                if alert.get("stage") != "completed" or alert.get("outcome") == "failed":
                    fields.extend(ERROR_FIELDS)
                # Error fields describe one error. Do not assemble a code from one
                # failed attempt and an unrelated message from another attempt.
                error_rows = [r for r in group["records"] if r.get("stage") != "billing" and has_error(r) and all(
                    not alert.get(name) or not r.get(name) or str(alert[name]) == str(r[name]) for name in ERROR_FIELDS)]
                expected = {name: {str(r[name]) for r in error_rows if r.get(name) not in (None, "")}
                            for name in ERROR_FIELDS[:3]}
                coherent = [r for r in error_rows if all(not values or values == {str(r.get(name, ""))}
                                                        for name, values in expected.items())]
                if error_rows and not coherent:
                    group["error_conflict"] = True
                    group["warnings"].append("上下文错误字段存在冲突或来自不同事件，未拼接为一个错误；请分析 error_evidence")
                for name in fields:
                    if alert.get(name):
                        continue
                    eligible = [r for r in (coherent if name in ERROR_FIELDS[:3] else group["records"])
                                if r.get(name) not in (None, "") and (name != "upstream_status" or r.get("stage") != "billing")]
                    values = {str(r[name]) for r in eligible}
                    if len(values) == 1:
                        alert[name] = values.pop()
                        provenance[name] = [r["event_id"] for r in eligible]
                    elif len(values) > 1:
                        group["warnings"].append("上下文中的 " + name + " 存在冲突，未自动补全")
            group["context_records"] = [context_record(r, secrets) for r in group.pop("records")]
            group["context_event_ids"] = [r["event_id"] for r in group["context_records"]]
            group["error_evidence"] = [r for r in group["context_records"] if has_error(r)]
            group["event_ids"] = [alert["event_id"]]
            group["field_sources"] = provenance
            associations.append(public_record(group, secrets))
        elif alert["event_id"] in previous:
            group = deepcopy(previous[alert["event_id"]])
            group["event_ids"] = [alert["event_id"]]
            associations.append(group)
            alert = deepcopy(original)
        enriched.append(public_record(alert, secrets) if key == "alerts" else alert)
    result[key] = enriched
    result["associations"] = associations
    result["enriched"] = True
    result["investigation"] = plan_investigation(result)
    return result
