"""Persistent notification outbox with leases and explicit delivery receipts."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from cloud_log_common import LogError
from cloud_log_reporting import public_record


def open_store(directory):
    path = Path(directory).expanduser()
    if not path.is_absolute():
        raise LogError("state-dir 必须是固定的绝对路径")
    path.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path / "outbox.sqlite3", timeout=30)
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("""CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY, connection_id TEXT NOT NULL, payload TEXT NOT NULL,
        created REAL NOT NULL, sent REAL, delivery_id TEXT, lease_until REAL,
        sending INTEGER NOT NULL DEFAULT 0)""")
    database.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
    database.execute("""CREATE TABLE IF NOT EXISTS target_receipts (
        event_id TEXT NOT NULL, destination TEXT NOT NULL, sent REAL NOT NULL,
        PRIMARY KEY(event_id, destination))""")
    database.execute("CREATE INDEX IF NOT EXISTS unsent_events ON events(sent, lease_until, created)")
    database.commit()
    return database


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
                database.execute("INSERT OR IGNORE INTO events(event_id, connection_id, payload, created) VALUES (?, ?, ?, ?)",
                                 (record["event_id"], record["connection_id"], json.dumps(safe, ensure_ascii=False), now))
            placeholders = ",".join("?" for _ in connection_ids)
            rows = database.execute(
                f"SELECT event_id, payload FROM events WHERE sent IS NULL AND (lease_until IS NULL OR lease_until < ?) "
                f"AND connection_id IN ({placeholders}) ORDER BY created, event_id LIMIT ?",
                (now, *connection_ids, batch_size)).fetchall()
            for event_id, _ in rows:
                database.execute("UPDATE events SET delivery_id=?, lease_until=?, sending=0 WHERE event_id=?",
                                 (delivery_id, now + lease_seconds, event_id))
            pending = database.execute(
                f"SELECT COUNT(*) FROM events WHERE sent IS NULL AND connection_id IN ({placeholders})", connection_ids).fetchone()[0]
            available = database.execute(
                f"SELECT COUNT(*) FROM events WHERE sent IS NULL AND (lease_until IS NULL OR lease_until < ?) "
                f"AND connection_id IN ({placeholders})", (now, *connection_ids)).fetchone()[0]
        return {"delivery_id": delivery_id if rows else None, "lease_seconds": lease_seconds,
                "alerts": [json.loads(payload) for _, payload in rows], "pending_count": pending,
                "available_count": available}
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
