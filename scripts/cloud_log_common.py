"""Portable configuration, timestamps and JSON helpers; no client integration."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


class LogError(Exception):
    """A diagnostic safe to display without HTTP bodies or credentials."""


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str, separators=(",", ":")).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(value, path=None):
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def load_env(path):
    if not path:
        return
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name.strip()):
            raise LogError(f"环境文件第 {number} 行格式错误")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(name.strip(), value)


def zone(value="+00:00"):
    if value in ("UTC", "Z"):
        return timezone.utc
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", value)
    if not match or int(match[2]) > 23 or int(match[3]) > 59:
        raise LogError("时区需为 UTC 或显式偏移，例如 +08:00")
    delta = timedelta(hours=int(match[2]), minutes=int(match[3]))
    return timezone(delta if match[1] == "+" else -delta)


def parse_time(value, default_zone=None, unit=None):
    if isinstance(value, (int, float)) or (unit and re.fullmatch(r"\d+(?:\.\d+)?", str(value))):
        factor = {"s": 1, "ms": 1000, "us": 1000000, "ns": 1000000000}.get(unit or "s")
        if factor is None:
            raise LogError("不支持的 timestamp_unit")
        return datetime.fromtimestamp(float(value) / factor, timezone.utc)
    text = re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d+)",
                  lambda m: m[1] + "." + m[2][:6].ljust(6, "0"), str(value).strip())
    result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if result.tzinfo is None:
        if default_zone is None:
            raise LogError("查询时间必须包含时区，例如 2026-10-17T11:00:00+08:00")
        result = result.replace(tzinfo=zone(default_zone))
    return result.astimezone(timezone.utc)


def config_connections(path, selected=None):
    config_path = Path(path).resolve()
    config = read_json(config_path)
    if config.get("schema_version") != 1 or not isinstance(config.get("connections"), list):
        raise LogError("连接配置需要 schema_version=1 和 connections 数组")
    connections = config["connections"]
    ids = [c.get("id") for c in connections]
    if len(ids) != len(set(ids)) or any(not isinstance(i, str) or not i for i in ids):
        raise LogError("每个连接必须有唯一且非空的 id")
    if selected and set(selected) - set(ids):
        raise LogError("指定连接不存在：" + ", ".join(sorted(set(selected) - set(ids))))
    result = []
    for original in connections:
        if selected and original["id"] not in selected:
            continue
        if not selected and not original.get("enabled", True):
            continue
        connection = dict(original)
        connection["_base_dir"] = str(config_path.parent)
        result.append(connection)
    if not result:
        raise LogError("未选择任何连接")
    return config, result


def get_profile(connection):
    if not connection.get("profile"):
        return {"parser": "generic", "timezone": "+00:00"}
    path = Path(connection["profile"])
    if not path.is_absolute():
        path = Path(connection["_base_dir"]) / path
    profile = read_json(path)
    if not isinstance(profile, dict) or profile.get("parser", "generic") not in {"generic", "gateway"}:
        raise LogError("服务 profile 需要对象，parser 为 generic 或 gateway")
    zone(profile.get("timezone", "+00:00"))
    if profile.get("timestamp_unit") not in (None, "s", "ms", "us", "ns"):
        raise LogError("服务 profile 的 timestamp_unit 无效")
    for flag in ("request_id_prefix", "prefer_level_prefix", "bracket_fields"):
        if flag in profile and type(profile[flag]) is not bool:
            raise LogError("profile." + flag + " 需要布尔值")
    for section in ("fields", "patterns", "stage_patterns", "level_map"):
        if not isinstance(profile.get(section, {}), dict):
            raise LogError("服务 profile 的 " + section + " 需要对象")
    for paths in profile.get("fields", {}).values():
        if not isinstance(paths, str) and not (isinstance(paths, list) and all(isinstance(p, str) for p in paths)):
            raise LogError("profile.fields 的值需要字段路径或路径数组")
    for section in ("patterns", "stage_patterns"):
        for pattern in profile.get(section, {}).values():
            try:
                expression = re.compile(pattern)
            except (re.error, TypeError):
                raise LogError("服务 profile 包含无效正则") from None
            if section == "patterns" and expression.groups < 1:
                raise LogError("profile.patterns 需要至少一个捕获组")
    if not isinstance(profile.get("json_payloads", []), list) or any(
            not isinstance(rule, dict) or not isinstance(rule.get("logger"), str) or
            not rule["logger"] or not isinstance(rule.get("prefix"), str) or not rule["prefix"] or
            ("target" in rule and (not isinstance(rule["target"], str) or
                                  not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", rule["target"])))
            for rule in profile.get("json_payloads", [])):
        raise LogError("profile.json_payloads 需要含 logger 和非空 prefix 的规则数组")
    return profile


def validate_connection(connection):
    for field, default in (("max_rows", 10000), ("max_pages", 100), ("timeout_seconds", 60)):
        value = connection.get(field, default)
        if type(value) is not int or value < 1:
            raise LogError(field + " 需要正整数")
    for key, value in connection.items():
        if key.endswith("_env") and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)):
            raise LogError("连接中的 *_env 需要有效环境变量名")
    if not isinstance(connection.get("containers", []), list) or any(not isinstance(x, str) for x in connection.get("containers", [])):
        raise LogError("containers 需要字符串数组")
    if connection.get("query_file"):
        path = Path(connection["query_file"])
        if not path.is_absolute():
            path = Path(connection["_base_dir"]) / path
        if not path.is_file():
            raise LogError("query_file 不存在")


def env_setting(connection, key, default_name=None, required=True):
    name = connection.get(key, default_name)
    value = os.getenv(name, "") if name else ""
    if required and not value.strip():
        raise LogError("缺少环境变量：" + str(name or key))
    return value.strip()


def credential_status(connection, defaults):
    return {connection.get(key, default): bool(os.getenv(connection.get(key, default), "").strip())
            for key, default in defaults.items()}


def custom_query(connection, start, end, limit, term):
    if not connection.get("query_file"):
        return None
    path = Path(connection["query_file"])
    if not path.is_absolute():
        path = Path(connection["_base_dir"]) / path
    template = path.read_text(encoding="utf-8-sig")
    if term and "{{term}}" not in template:
        raise LogError("带检索词的自定义查询必须包含 {{term}} 占位符")
    text = template
    for key, value in {"start": start.isoformat(), "end": end.isoformat(),
                       "limit": str(limit), "term": json.dumps(term or "", ensure_ascii=False)}.items():
        text = text.replace("{{" + key + "}}", value)
    return text
