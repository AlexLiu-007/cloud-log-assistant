"""Persistent notification outbox with leases and explicit delivery receipts."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cloud_log_common import LogError
from cloud_log_reporting import public_record


AUTH_SCHEME = re.compile(r"^(?:Bearer|Basic|Token|ApiKey)\s+", re.I)
MASKED_AUTH = re.compile(r"(?:[^*]{4})?\*{8}(?:[^*]{4})?")


def open_store(directory):
    path = Path(directory).expanduser()
    if not path.is_absolute():
        raise LogError("state-dir 必须是固定的绝对路径")
    path.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path / "outbox.sqlite3", timeout=30)
    try:
        database.execute("PRAGMA journal_mode=WAL")
        with database:
            database.execute("BEGIN IMMEDIATE")
            database.execute("""CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, connection_id TEXT NOT NULL, payload TEXT NOT NULL,
                created REAL NOT NULL, sent REAL, delivery_id TEXT, lease_until REAL,
                sending INTEGER NOT NULL DEFAULT 0, authorization_fingerprint TEXT)""")
            event_columns = {row[1] for row in database.execute("PRAGMA table_info(events)")}
            if "authorization_fingerprint" not in event_columns:
                database.execute("ALTER TABLE events ADD COLUMN authorization_fingerprint TEXT")
            for name, declaration in (("notify_after", "REAL NOT NULL DEFAULT 0"),
                                      ("notification_payload", "TEXT"), ("notification_group", "TEXT")):
                if name not in event_columns:
                    database.execute(f"ALTER TABLE events ADD COLUMN {name} {declaration}")
            database.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            database.execute("""CREATE TABLE IF NOT EXISTS target_receipts (
                event_id TEXT NOT NULL, destination TEXT NOT NULL, sent REAL NOT NULL,
                PRIMARY KEY(event_id, destination))""")
            database.execute("""CREATE TABLE IF NOT EXISTS scan_cursors (
                connection_id TEXT NOT NULL, scope_hash TEXT NOT NULL, next_start REAL NOT NULL,
                updated REAL NOT NULL, PRIMARY KEY(connection_id, scope_hash))""")
            database.execute("CREATE INDEX IF NOT EXISTS unsent_events ON events(sent, lease_until, created)")
            database.execute("CREATE INDEX IF NOT EXISTS notification_group_events ON events(notification_group, sent)")
            database.execute("""CREATE TABLE IF NOT EXISTS notification_history (
                destination TEXT NOT NULL, group_id TEXT NOT NULL, last_sent REAL NOT NULL, level INTEGER NOT NULL,
                PRIMARY KEY(destination,group_id))""")
            database.execute("""CREATE TABLE IF NOT EXISTS notification_deferrals (
                event_id TEXT NOT NULL, destination TEXT NOT NULL, group_id TEXT NOT NULL, ready_at REAL NOT NULL,
                PRIMARY KEY(event_id,destination))""")
            database.execute("""CREATE TABLE IF NOT EXISTS notification_plans (
                plan_id TEXT PRIMARY KEY, destination TEXT NOT NULL, payloads TEXT NOT NULL, event_ids TEXT NOT NULL,
                groups_json TEXT NOT NULL, next_part INTEGER NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, report_id TEXT)""")
            if "report_id" not in {row[1] for row in database.execute("PRAGMA table_info(notification_plans)")}:
                database.execute("ALTER TABLE notification_plans ADD COLUMN report_id TEXT")
            database.execute("""CREATE TABLE IF NOT EXISTS notification_plan_events (
                plan_id TEXT NOT NULL, event_id TEXT NOT NULL, PRIMARY KEY(plan_id,event_id))""")
            database.execute("CREATE INDEX IF NOT EXISTS notification_event_plans ON notification_plan_events(event_id)")
            database.execute("CREATE INDEX IF NOT EXISTS notification_open_plans ON notification_plans(complete,created)")
            database.execute("CREATE INDEX IF NOT EXISTS notification_due_events ON events(sent,notify_after,created)")
        return database
    except Exception:
        database.close()
        raise


def _authorization_value(value):
    if not isinstance(value, (str, int)):
        return None
    value = str(value).strip()
    value = AUTH_SCHEME.sub("", value, count=1).strip()
    if not value or value.casefold() in {"未记录", "不适用", "[已隐藏]"} or MASKED_AUTH.fullmatch(value):
        return None
    return value


def _authorization_fingerprint(database, value):
    value = _authorization_value(value)
    if value is None:
        return None
    key = secrets.token_hex(32)
    database.execute("INSERT OR IGNORE INTO settings(name, value) VALUES ('authorization_hmac_key', ?)", (key,))
    key = database.execute("SELECT value FROM settings WHERE name='authorization_hmac_key'").fetchone()[0]
    return hmac.new(bytes.fromhex(key), value.encode("utf-8"), hashlib.sha256).hexdigest()


def load_authorization_fingerprints(directory, event_ids):
    """Return state-scoped authorization fingerprints without exposing credential values."""
    event_ids = list(dict.fromkeys(event_ids))
    if not event_ids:
        return {}
    database = open_store(directory)
    try:
        result = {}
        for offset in range(0, len(event_ids), 500):
            batch = event_ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = database.execute(
                f"SELECT event_id, authorization_fingerprint FROM events "
                f"WHERE event_id IN ({placeholders}) AND authorization_fingerprint IS NOT NULL", batch)
            result.update(rows.fetchall())
        return result
    finally:
        database.close()


def enqueue_and_claim(directory, records, connection_ids, lease_seconds=900, batch_size=100, extra_secrets=()):
    if lease_seconds < 30 or batch_size < 1:
        raise LogError("lease-seconds 至少 30，batch-size 至少 1")
    database = open_store(directory)
    now, delivery_id = time.time(), str(uuid.uuid4())
    try:
        with database:
            database.execute("BEGIN IMMEDIATE")
            for record in records:
                safe = public_record(record, extra_secrets)
                fingerprint = _authorization_fingerprint(database, record.get("authorization"))
                database.execute(
                    "INSERT OR IGNORE INTO events(event_id, connection_id, payload, created, authorization_fingerprint) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (record["event_id"], record["connection_id"], json.dumps(safe, ensure_ascii=False), now,
                     fingerprint))
                if fingerprint:
                    database.execute(
                        "UPDATE events SET authorization_fingerprint=COALESCE(authorization_fingerprint, ?) "
                        "WHERE event_id=?", (fingerprint, record["event_id"]))
            placeholders = ",".join("?" for _ in connection_ids)
            from cloud_log_notify_state import frozen_claim
            rows, _ = frozen_claim(database, connection_ids, now)
            if not rows:
                rows = database.execute(
                    f"SELECT event_id, payload, notification_payload FROM events WHERE sent IS NULL "
                    f"AND notify_after<=? AND (lease_until IS NULL OR lease_until < ?) "
                    f"AND NOT EXISTS (SELECT 1 FROM notification_plan_events pe JOIN notification_plans p "
                    f"ON pe.plan_id=p.plan_id WHERE pe.event_id=events.event_id AND p.complete=0) "
                    f"AND connection_id IN ({placeholders}) ORDER BY created, event_id LIMIT ?",
                    (now, now, *connection_ids, batch_size)).fetchall()
            for event_id, _, _ in rows:
                database.execute("UPDATE events SET delivery_id=?, lease_until=?, sending=0 WHERE event_id=?",
                                 (delivery_id, now + lease_seconds, event_id))
            pending = database.execute(
                f"SELECT COUNT(*) FROM events WHERE sent IS NULL AND connection_id IN ({placeholders})", connection_ids).fetchone()[0]
            available = database.execute(
                f"SELECT COUNT(*) FROM events WHERE sent IS NULL AND notify_after<=? AND (lease_until IS NULL OR lease_until < ?) "
                f"AND NOT EXISTS (SELECT 1 FROM notification_plan_events pe JOIN notification_plans p "
                f"ON pe.plan_id=p.plan_id WHERE pe.event_id=events.event_id AND p.complete=0) "
                f"AND connection_id IN ({placeholders})", (now, now, *connection_ids)).fetchone()[0]
            retry_rows, _ = frozen_claim(database, connection_ids, now)
            available += len(retry_rows)
            deferred = database.execute(
                f"SELECT COUNT(*), MIN(notify_after) FROM events WHERE sent IS NULL AND notify_after>? "
                f"AND connection_id IN ({placeholders})", (now, *connection_ids)).fetchone()
        alerts, associations, analyses = [], [], {}
        for event_id, payload, saved in rows:
            context = json.loads(saved) if saved else {}
            alerts.append(context.get("record", json.loads(payload)))
            if context.get("association"):
                associations.append({**context["association"], "event_ids": [event_id]})
            if context.get("analysis"):
                analyses[event_id] = context["analysis"]
        return {"delivery_id": delivery_id if rows else None, "lease_seconds": lease_seconds,
                "alerts": alerts, "associations": associations, "notification_analyses": analyses,
                "pending_count": pending, "available_count": available,
                "deferred_count": deferred[0], "next_notification_at": deferred[1]}
    finally:
        database.close()


def load_scan_cursors(directory, scopes, initial_start):
    """Create stable per-source checkpoints and return their next query starts."""
    database = open_store(directory)
    now, initial = time.time(), initial_start.timestamp()
    try:
        with database:
            for connection_id, scope_hash in scopes.items():
                database.execute("DELETE FROM scan_cursors WHERE connection_id=? AND scope_hash<>?",
                                 (connection_id, scope_hash))
                database.execute(
                    "INSERT OR IGNORE INTO scan_cursors(connection_id, scope_hash, next_start, updated) VALUES (?, ?, ?, ?)",
                    (connection_id, scope_hash, initial, now))
            rows = {connection_id: database.execute(
                "SELECT next_start FROM scan_cursors WHERE connection_id=? AND scope_hash=?",
                (connection_id, scope_hash)).fetchone()[0]
                    for connection_id, scope_hash in scopes.items()}
        return {connection_id: datetime.fromtimestamp(next_start, timezone.utc)
                for connection_id, next_start in rows.items()}
    finally:
        database.close()


def advance_scan_cursors(directory, scopes, end):
    """Advance only sources whose whole interval completed successfully."""
    if not scopes:
        return 0
    database = open_store(directory)
    now, target = time.time(), end.timestamp()
    try:
        with database:
            updated = 0
            for connection_id, scope_hash in scopes.items():
                cursor = database.execute(
                    "UPDATE scan_cursors SET next_start=MAX(next_start, ?), updated=? "
                    "WHERE connection_id=? AND scope_hash=?",
                    (target, now, connection_id, scope_hash))
                updated += cursor.rowcount
        return updated
    finally:
        database.close()


def purge_sent(directory, retention_days):
    """Remove only acknowledged events older than the explicitly chosen retention."""
    if type(retention_days) is not int or retention_days < 1:
        raise LogError("retention-days 必须是至少 1 天的整数")
    database = open_store(directory)
    cutoff = time.time() - retention_days * 86400
    try:
        with database:
            database.execute("DELETE FROM notification_deferrals WHERE event_id IN "
                             "(SELECT event_id FROM events WHERE sent IS NOT NULL AND sent < ?)", (cutoff,))
            database.execute("DELETE FROM notification_plan_events WHERE plan_id IN "
                             "(SELECT plan_id FROM notification_plans WHERE complete=1 AND created < ?)", (cutoff,))
            database.execute("DELETE FROM notification_plans WHERE complete=1 AND created < ?", (cutoff,))
            database.execute("DELETE FROM notification_history WHERE last_sent < ?", (cutoff,))
            database.execute(
                "DELETE FROM target_receipts WHERE event_id IN "
                "(SELECT event_id FROM events WHERE sent IS NOT NULL AND sent < ?)", (cutoff,))
            receipts = database.execute("SELECT changes()").fetchone()[0]
            events = database.execute(
                "DELETE FROM events WHERE sent IS NOT NULL AND sent < ?", (cutoff,)).rowcount
        return {"purged_events": events, "purged_target_receipts": receipts,
                "retention_days": retention_days,
                "cutoff": datetime.fromtimestamp(cutoff, timezone.utc).isoformat()}
    finally:
        database.close()


def receipt(directory, delivery_id, sent):
    database = open_store(directory)
    try:
        with database:
            if sent:
                cursor = database.execute(
                    "UPDATE events SET sent=?, lease_until=NULL, sending=0 WHERE delivery_id=? AND sent IS NULL",
                    (time.time(), delivery_id))
            else:
                cursor = database.execute(
                    "UPDATE events SET delivery_id=NULL, lease_until=NULL, sending=0 WHERE delivery_id=? AND sent IS NULL", (delivery_id,))
        return {"delivery_id": delivery_id, "action": "ack" if sent else "release", "affected": cursor.rowcount}
    finally:
        database.close()


def begin_send(directory, delivery_id, event_ids, destination, previous_destinations=()):
    database = open_store(directory)
    try:
        with database:
            database.execute("BEGIN IMMEDIATE")
            rows = database.execute(
                "SELECT event_id, lease_until, sending FROM events WHERE delivery_id=? AND sent IS NULL",
                (delivery_id,)).fetchall()
            if not rows or len(event_ids) != len(set(event_ids)) or {row[0] for row in rows} != set(event_ids):
                raise LogError("投递批次失效或事件集合不一致；请重新 scan")
            if any(row[2] or not row[1] or row[1] < time.time() for row in rows):
                raise LogError("投递批次已在发送中或租约过期；请检查投递状态")
            binding = database.execute("SELECT value FROM settings WHERE name='destination'").fetchone()
            if binding and binding[0] in previous_destinations:
                database.execute("UPDATE settings SET value=? WHERE name='destination'", (destination,))
            elif binding and binding[0] != destination:
                raise LogError("此状态目录已绑定其他通知目标；为不同目标使用独立状态目录")
            database.execute("INSERT OR IGNORE INTO settings(name,value) VALUES ('destination',?)", (destination,))
            database.execute("UPDATE events SET sending=1, lease_until=? WHERE delivery_id=? AND sent IS NULL",
                             (time.time() + 900, delivery_id))
    finally:
        database.close()


def renew_send(directory, delivery_id, event_count, lease_seconds=900):
    """Refresh ownership before each network request in a long delivery."""
    database = open_store(directory)
    try:
        with database:
            database.execute("BEGIN IMMEDIATE")
            now = time.time()
            cursor = database.execute(
                "UPDATE events SET lease_until=? WHERE delivery_id=? AND sent IS NULL AND sending=1 AND lease_until>=?",
                (now + lease_seconds, delivery_id, now))
            if cursor.rowcount != event_count:
                raise LogError("投递租约已失效，停止发送；检查其他投递流程")
    finally:
        database.close()


def pending_for_target(directory, event_ids, destination):
    if not event_ids:
        return set()
    database = open_store(directory)
    try:
        placeholders = ",".join("?" for _ in event_ids)
        delivered = {row[0] for row in database.execute(
            f"SELECT event_id FROM target_receipts WHERE destination=? AND event_id IN ({placeholders})", (destination, *event_ids))}
        return set(event_ids) - delivered
    finally:
        database.close()


def confirm_target(directory, delivery_id, event_ids, destination):
    database = open_store(directory)
    try:
        with database:
            database.execute("BEGIN IMMEDIATE")
            owned = {row[0] for row in database.execute(
                "SELECT event_id FROM events WHERE delivery_id=? AND sent IS NULL AND sending=1 AND lease_until>=?",
                (delivery_id, time.time()))}
            if not set(event_ids) <= owned:
                raise LogError("目标已接收，但本地投递租约失效；检查投递状态")
            database.executemany("INSERT OR IGNORE INTO target_receipts(event_id,destination,sent) VALUES (?,?,?)",
                                 [(event_id, destination, time.time()) for event_id in event_ids])
    finally:
        database.close()
