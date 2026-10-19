"""Durable repeat windows, redacted reports, and frozen per-target card plans."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from cloud_log_common import LogError
from cloud_log_reporting import collect_secrets, redact


def settings(channel):
    values = {"repeat_interval_seconds": channel.get("repeat_interval_seconds", 900),
              "repeat_escalation_count": channel.get("repeat_escalation_count", 100)}
    for key, maximum in (("repeat_interval_seconds", 86400), ("repeat_escalation_count", 1000000)):
        if type(values[key]) is not int or not 0 <= values[key] <= maximum:
            raise LogError(key + f" 需要 0 到 {maximum} 的整数；0 表示关闭")
    return values


def safe_data(data, extra_secrets=()):
    secrets = collect_secrets(data) | set(extra_secrets)
    def strip(item):
        if isinstance(item, dict):
            return {key: strip(child) for key, child in item.items() if key != "raw"}
        if isinstance(item, list):
            return [strip(child) for child in item]
        return item
    return redact(strip(data), secrets)


def save_report(directory, document, analyses, groups, secrets=()):
    report_id = uuid.uuid4().hex[:20]
    data = safe_data({"document": document, "analyses": analyses,
                      "groups": [{"id": group["id"], "event_ids": [row["event_id"] for row in group["records"]],
                                  "classification_issues": group["classification_issues"]}
                                 for group in groups]}, secrets)
    path = Path(directory) / "reports"
    path.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path / (report_id + ".json"))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return report_id


def read_report(directory, report_id):
    if not re.fullmatch(r"[a-f0-9]{20}", report_id):
        raise LogError("详情编号格式无效")
    path = Path(directory)
    if not path.is_absolute():
        raise LogError("state-dir 必须是固定的绝对路径")
    try:
        return json.loads((path / "reports" / (report_id + ".json")).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LogError("该状态目录中没有此详情编号") from None


def frozen_claim(database, connection_ids, now):
    """Claim connected unfinished plans together, even after the batch size changes."""
    memberships = {}
    for plan_id, event_id in database.execute(
            "SELECT p.plan_id, e.event_id FROM notification_plans p JOIN notification_plan_events e "
            "ON p.plan_id=e.plan_id WHERE p.complete=0 ORDER BY p.created, p.plan_id"):
        memberships.setdefault(plan_id, set()).add(event_id)
    reserved = set().union(*memberships.values()) if memberships else set()
    examined = set()
    for ids in memberships.values():
        if ids <= examined:
            continue
        component = set(ids)
        while True:
            expanded = component | set().union(*(items for items in memberships.values() if items & component))
            if expanded == component:
                break
            component = expanded
        examined |= component
        rows = [database.execute("SELECT event_id, payload, notification_payload, connection_id, lease_until, sent "
                                 "FROM events WHERE event_id=?", (event_id,)).fetchone() for event_id in sorted(component)]
        if all(row and row[3] in connection_ids and (row[4] is None or row[4] < now) and row[5] is None for row in rows):
            return [(row[0], row[1], row[2]) for row in rows], reserved
    return [], reserved


def snapshots(directory, delivery_id, groups, secrets=()):
    from cloud_log_state import open_store
    database = open_store(directory)
    try:
        with database:
            for group in groups:
                for record in group["records"]:
                    event_id = record["event_id"]
                    data = safe_data({"record": record, "association": group["associations"][event_id],
                                      "analysis": group["analyses"][event_id]}, secrets)
                    changed = database.execute(
                        "UPDATE events SET notification_payload=?, notification_group=? "
                        "WHERE event_id=? AND delivery_id=? AND sending=1 AND sent IS NULL",
                        (json.dumps(data, ensure_ascii=False), group["id"], event_id, delivery_id)).rowcount
                    if changed != 1:
                        raise LogError("保存通知上下文时批次已失效")
    finally:
        database.close()


def select_due(directory, target, groups, policy, bypass=False):
    from cloud_log_state import open_store
    database = open_store(directory)
    now, due, delayed = time.time(), [], []
    try:
        with database:
            database.execute("BEGIN IMMEDIATE")
            for group in groups:
                ids = [row["event_id"] for row in group["records"]]
                history = database.execute(
                    "SELECT last_sent, level FROM notification_history WHERE destination=? AND group_id=?",
                    (target, group["id"])).fetchone()
                expired = any(database.execute(
                    "SELECT 1 FROM notification_deferrals WHERE event_id=? AND destination=? AND group_id=? AND ready_at<=?",
                    (event_id, target, group["id"], now)).fetchone() for event_id in ids)
                pending = database.execute(
                    "SELECT COUNT(*) FROM events e WHERE e.notification_group=? AND e.sent IS NULL "
                    "AND NOT EXISTS(SELECT 1 FROM target_receipts r WHERE r.event_id=e.event_id AND r.destination=?)",
                    (group["id"], target)).fetchone()[0]
                escalate = policy["repeat_escalation_count"] and pending >= policy["repeat_escalation_count"]
                defer = (not bypass and group.get("cooldown_eligible", False) and group["mergeable"] and not group["classification_issues"]
                         and history and policy["repeat_interval_seconds"] and not expired and not escalate
                         and group["level"] < 5 and group["level"] <= history[1]
                         and now < history[0] + policy["repeat_interval_seconds"])
                if defer:
                    deadline = history[0] + policy["repeat_interval_seconds"]
                    database.executemany(
                        "INSERT INTO notification_deferrals(event_id,destination,group_id,ready_at) VALUES (?,?,?,?) "
                        "ON CONFLICT(event_id,destination) DO UPDATE SET group_id=excluded.group_id, "
                        "ready_at=MIN(notification_deferrals.ready_at,excluded.ready_at)",
                        [(event_id, target, group["id"], deadline) for event_id in ids])
                    delayed.extend(ids)
                else:
                    due.append(group)
                    if escalate:
                        database.execute("UPDATE notification_deferrals SET ready_at=? WHERE destination=? AND group_id=?",
                                         (now, target, group["id"]))
                        database.execute("UPDATE events SET notify_after=MIN(notify_after,?) WHERE notification_group=? AND sent IS NULL",
                                         (now, group["id"]))
        return due, delayed
    finally:
        database.close()


def active_plans(directory, target, event_ids):
    from cloud_log_state import open_store
    database = open_store(directory)
    try:
        plans = []
        for plan_id, payloads, ids, groups, part, report_id in database.execute(
                "SELECT plan_id,payloads,event_ids,groups_json,next_part,report_id FROM notification_plans "
                "WHERE destination=? AND complete=0 ORDER BY created", (target,)):
            ids = json.loads(ids)
            if set(ids) & set(event_ids):
                if not set(ids) <= set(event_ids):
                    raise LogError("重试卡片包含本批次之外的事件；请 release 后按同一连接范围重新 claim")
                plans.append({"id": plan_id, "parts": json.loads(payloads), "event_ids": ids,
                              "groups": json.loads(groups), "next_part": part, "report_id": report_id})
        return plans
    finally:
        database.close()


def create_plan(directory, target, groups, parts, report_id=None):
    from cloud_log_state import open_store
    ids = [row["event_id"] for group in groups for row in group["records"]]
    plan = {"id": uuid.uuid4().hex, "parts": parts, "event_ids": ids,
            "groups": [{"id": group["id"], "level": group["level"]} for group in groups], "next_part": 0, "report_id": report_id}
    database = open_store(directory)
    try:
        with database:
            database.execute("INSERT INTO notification_plans(plan_id,destination,payloads,event_ids,groups_json,created,report_id) "
                             "VALUES (?,?,?,?,?,?,?)", (plan["id"], target, json.dumps(parts, ensure_ascii=False),
                                                       json.dumps(ids), json.dumps(plan["groups"]), time.time(), report_id))
            database.executemany("INSERT INTO notification_plan_events(plan_id,event_id) VALUES (?,?)",
                                 [(plan["id"], event_id) for event_id in ids])
        return plan
    finally:
        database.close()


def confirm_part(directory, delivery_id, target, plan, index):
    from cloud_log_state import open_store
    database = open_store(directory)
    now = time.time()
    try:
        with database:
            database.execute("BEGIN IMMEDIATE")
            owned = {row[0] for row in database.execute(
                "SELECT event_id FROM events WHERE delivery_id=? AND sending=1 AND sent IS NULL AND lease_until>=?",
                (delivery_id, now))}
            if not set(plan["event_ids"]) <= owned:
                raise LogError("飞书已接收片段，但本地租约失效；检查投递状态")
            changed = database.execute("UPDATE notification_plans SET next_part=? WHERE plan_id=? AND next_part=? AND complete=0",
                                       (index + 1, plan["id"], index)).rowcount
            if changed != 1:
                raise LogError("卡片分段确认状态冲突")
            if index + 1 == len(plan["parts"]):
                database.executemany("INSERT OR IGNORE INTO target_receipts(event_id,destination,sent) VALUES (?,?,?)",
                                     [(event_id, target, now) for event_id in plan["event_ids"]])
                for group in plan["groups"]:
                    database.execute("INSERT INTO notification_history(destination,group_id,last_sent,level) VALUES (?,?,?,?) "
                                     "ON CONFLICT(destination,group_id) DO UPDATE SET last_sent=excluded.last_sent,level=excluded.level",
                                     (target, group["id"], now, group["level"]))
                database.executemany("DELETE FROM notification_deferrals WHERE event_id=? AND destination=?",
                                     [(event_id, target) for event_id in plan["event_ids"]])
                database.execute("UPDATE notification_plans SET complete=1 WHERE plan_id=?", (plan["id"],))
    finally:
        database.close()


def finish_delivery(directory, delivery_id, targets):
    from cloud_log_state import open_store
    database = open_store(directory)
    now, acknowledged, deferred = time.time(), 0, 0
    deadlines = []
    try:
        with database:
            database.execute("BEGIN IMMEDIATE")
            rows = list(database.execute(
                "SELECT event_id,sending,lease_until FROM events WHERE delivery_id=? AND sent IS NULL", (delivery_id,)))
            if not rows or any(not row[1] or not row[2] or row[2] < now for row in rows):
                raise LogError("结束投递时批次租约已失效；检查投递状态")
            ids = [row[0] for row in rows]
            for event_id in ids:
                completed = {row[0] for row in database.execute(
                    "SELECT destination FROM target_receipts WHERE event_id=?", (event_id,))}
                pending = set(targets) - completed
                if not pending:
                    database.execute("UPDATE events SET sent=?,sending=0,lease_until=NULL,notify_after=0 WHERE event_id=?", (now, event_id))
                    acknowledged += 1
                else:
                    times = [database.execute("SELECT ready_at FROM notification_deferrals WHERE event_id=? AND destination=?",
                                              (event_id, target)).fetchone() for target in pending]
                    ready_at = min(row[0] if row else 0 for row in times)
                    database.execute("UPDATE events SET sending=0,lease_until=NULL,delivery_id=NULL,notify_after=? WHERE event_id=?",
                                     (ready_at, event_id))
                    deferred += 1
                    deadlines.append(ready_at)
        return {"acknowledged_events": acknowledged, "deferred_events": deferred,
                "next_notification_at": min(deadlines) if deadlines else None}
    finally:
        database.close()
