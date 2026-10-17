"""Fill missing alert fields from explicit identifiers without changing a delivery batch."""
from __future__ import annotations

from copy import deepcopy

from cloud_log_common import LogError
from cloud_log_correlation import correlate
from cloud_log_reporting import collect_secrets, public_record

IDENTIFIERS = ("request_id", "trace_id", "correlation_id")
METADATA = ("actor", "authorization", "method", "path", "model", "upstream_provider",
            "upstream_channel", "upstream_model", "operation")
ERROR_FIELDS = ("error_code", "error_type", "error_message", "upstream_status")


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
        candidates, consulted = {alert["event_id"]: alert}, []
        if field:
            for context in contexts:
                matching = [r for r in context.get("records", context.get("alerts", []))
                            if (r.get("connection_id"), r.get("resource", "")) ==
                               (alert["connection_id"], alert.get("resource", "")) and r.get(field) == alert[field]]
                if matching:
                    candidates.update((r["event_id"], r) for r in matching)
                    sources = [s for s in context.get("sources", []) if
                               (s.get("connection_id"), s.get("resource", "")) ==
                               (alert["connection_id"], alert.get("resource", ""))]
                    consulted.append(all(s.get("complete") is True and s.get("status") != "failed" for s in sources)
                                     if sources else context.get("complete") is True)
        if field and consulted:
            group = correlate(list(candidates.values()), alert["event_id"], field=field, complete=all(consulted))
            provenance = {}
            if not group["ambiguous"]:
                fields = list(METADATA)
                if alert.get("stage") != "completed" or alert.get("outcome") == "failed":
                    fields.extend(ERROR_FIELDS)
                for name in fields:
                    if alert.get(name):
                        continue
                    eligible = [r for r in group["records"] if r.get(name) not in (None, "") and
                                (name not in ERROR_FIELDS or r.get("stage") != "billing")]
                    values = {str(r[name]) for r in eligible}
                    if len(values) == 1:
                        alert[name] = values.pop()
                        provenance[name] = [r["event_id"] for r in eligible]
                    elif len(values) > 1:
                        group["warnings"].append("上下文中的 " + name + " 存在冲突，未自动补全")
            group["context_event_ids"] = [r["event_id"] for r in group.pop("records")]
            group["event_ids"] = [alert["event_id"]]
            group["field_sources"] = provenance
            associations.append(group)
        elif alert["event_id"] in previous:
            group = deepcopy(previous[alert["event_id"]])
            group["event_ids"] = [alert["event_id"]]
            associations.append(group)
        enriched.append(public_record(alert, secrets) if key == "alerts" else alert)
    result[key] = enriched
    result["associations"] = associations
    result["enriched"] = True
    return result
