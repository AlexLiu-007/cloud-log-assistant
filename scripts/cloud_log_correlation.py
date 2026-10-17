"""Evidence-based association without inventing request IDs."""
from __future__ import annotations

from cloud_log_common import LogError, parse_time

IDENTIFIERS = ("request_id", "trace_id", "correlation_id", "business_id")


def correlate(records, anchor_id, *, field=None, cross_connections=False,
              window_seconds=30, heuristic_fields=("path", "actor", "model"), complete=True):
    anchor = next((record for record in records if record["event_id"] == anchor_id), None)
    if anchor is None:
        raise LogError("锚点 event_id 不在输入记录中")
    candidates = [r for r in records if cross_connections or
                  (r["connection_id"], r.get("resource", "")) == (anchor["connection_id"], anchor.get("resource", ""))]
    field = field or next((key for key in IDENTIFIERS if anchor.get(key)), None)
    evidence, chosen = [], []
    if field and field not in IDENTIFIERS:
        raise LogError("只能使用请求、追踪、关联或业务标识做标识匹配")
    if field and not anchor.get(field):
        raise LogError("锚点缺少指定关联字段")
    if field:
        chosen = [r for r in candidates if r.get(field) == anchor[field]]
        mode = "business" if field == "business_id" else "identifier"
        evidence.append(f"{field} 精确匹配；" + ("已显式启用跨来源匹配" if cross_connections else "限同一连接和资源"))
    else:
        mode = "context"
        chosen = [anchor]
        if not cross_connections and anchor.get("timestamp") and anchor.get("instance"):
            for row in candidates:
                if row["event_id"] == anchor_id or row.get("instance") != anchor["instance"] or not row.get("timestamp"):
                    continue
                if row.get("service") != anchor.get("service") or row.get("environment") != anchor.get("environment"):
                    continue
                # Conflicting explicit identities are never joined by weak signals.
                if any(anchor.get(key) and row.get(key) and anchor[key] != row[key] for key in IDENTIFIERS):
                    continue
                matched = [key for key in heuristic_fields if anchor.get(key) and row.get(key) == anchor[key]]
                if not matched:
                    continue
                if abs((parse_time(row["timestamp"]) - parse_time(anchor["timestamp"])).total_seconds()) <= window_seconds:
                    chosen.append(row)
                    evidence.append(row["event_id"] + "：同实例且 " + ", ".join(matched) + f" 相同，距锚点不超过 {window_seconds} 秒")
        if len(chosen) > 1:
            mode = "heuristic"
        else:
            evidence.append("关联信息不足，只保留锚点，不能确认同一次请求")
    unique = {record["event_id"]: record for record in chosen}
    chosen = sorted(unique.values(), key=lambda r: (r.get("timestamp") or "", r["event_id"]))
    starts = [r for r in chosen if r.get("stage") == "received"]
    ends = [r for r in chosen if r.get("stage") == "completed"]
    warnings, outcome = [], "unknown"
    timestamps_present = all(r.get("timestamp") for r in chosen)
    if not timestamps_present:
        warnings.append("部分记录缺少有效时间，无法确认完整时间线")
    if not complete:
        warnings.append("查询结果不完整")
    if not starts:
        warnings.append("未观察到请求入口")
    if not ends:
        warnings.append("未观察到最终响应")
    identities = {r["request_id"] for r in chosen if r.get("request_id")}
    ambiguous = len(starts) > 1 or len(identities) > 1 or len(ends) > 1
    if ambiguous:
        warnings.append("多个入口、请求 ID 或结束事件，不能认定为单次请求")
    if mode == "identifier" and len(ends) == 1 and not ambiguous:
        outcome = ends[0].get("outcome", "unknown")
    if mode == "heuristic":
        warnings.append("上下文匹配仅为推测，不据此判定最终请求结果")
    if mode == "business":
        warnings.append("同一业务对象可能包含多次请求")
    stream_errors = [r for r in chosen if r.get("stage") == "stream" and
                     (r.get("error_message") or r.get("level") in {"ERROR", "CRITICAL", "FATAL"})]
    if outcome == "success" and stream_errors:
        outcome = "stream_error"
        warnings.append("结束事件记录成功，但观察到流处理异常")
    bounded = mode == "identifier" and len(starts) == len(ends) == 1 and not ambiguous and complete and timestamps_present
    return {"mode": mode, "field": field, "value": anchor.get(field, "") if field else "",
            "anchor_event_id": anchor_id, "evidence": evidence, "warnings": warnings,
            "completeness": "observed_boundaries" if bounded else "unconfirmed",
            "outcome": outcome, "ambiguous": ambiguous, "records": chosen}
