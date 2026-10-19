"""Final-output redaction and channel-neutral notification rendering."""
from __future__ import annotations

import json
import re

from cloud_log_parsing import SECRET_NAMES, bracket_fields, json_like, key_name

SCHEME = re.compile(r"^(Bearer|Basic|Token|ApiKey)\s+(.+)$", re.I | re.S)
BEARER = re.compile(r"\b(?:Bearer|Basic|Token|ApiKey)\s+[A-Za-z0-9.*_~+/=-]+", re.I)
SECRET_PATTERN = "|".join(re.escape(name).replace("_", "[-_]?") for name in sorted(SECRET_NAMES, key=len, reverse=True))
LABELED = re.compile(
    r"""(?i)(?<![\w])(?:["']?)(?P<name>""" + SECRET_PATTERN +
    r""")["']?\s*[:=]\s*(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\[[^\]\r\n]*\]|(?:Bearer|Basic|Token|ApiKey)\s+[^\s,;}\]"']+|[^\s,;}\]"']+)""")


def already_masked(value):
    return bool(re.fullmatch(r"(?:[^*]{4})?\*{8}(?:[^*]{4})?", value))


def mask(value):
    value = str(value).strip()
    scheme = SCHEME.match(value)
    if scheme:
        return scheme[1] + " " + mask(scheme[2])
    if not value or value in {"未记录", "不适用", "[已隐藏]"}:
        return value
    if already_masked(value):
        return value
    return "********" if len(value) <= 12 else value[:4] + "********" + value[-4:]


def clean_secret(value):
    value = value.strip()
    if len(value) >= 2 and ((value[0] == value[-1] and value[0] in "\"'") or (value[0] == "[" and value[-1] == "]")):
        value = value[1:-1]
    parsed = json_like(value)
    return parsed if isinstance(parsed, str) else value


def collect_secrets(value):
    found = set()

    def add(item, depth=0):
        if depth > 20:
            return
        if isinstance(item, (list, dict)):
            for child in (item.values() if isinstance(item, dict) else item):
                add(child, depth + 1)
            return
        if isinstance(item, (str, int)) and str(item).strip():
            token = clean_secret(str(item))
            if not already_masked(token):
                found.add(token)
                scheme = SCHEME.match(token)
                if scheme:
                    found.add(scheme[2])

    def walk(item, depth=0):
        if depth > 20:
            return
        if isinstance(item, dict):
            # Some HTTP libraries serialize headers as [{name, value}].
            named = {key_name(key): child for key, child in item.items()}
            if key_name(named.get("name", "")) in SECRET_NAMES and "value" in named:
                add(named["value"])
            for key, child in item.items():
                if key_name(key) in SECRET_NAMES:
                    add(child)
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
        elif isinstance(item, str):
            parsed = json_like(item) if item.lstrip().startswith(("{", "[", '"')) else item
            if isinstance(parsed, (dict, list)):
                walk(parsed, depth + 1)
            fields, _ = bracket_fields(item)
            for key, child in fields.items():
                if key in SECRET_NAMES:
                    add(child)
                else:
                    decoded = json_like(child)
                    if isinstance(decoded, (dict, list)):
                        walk(decoded, depth + 1)
            for match in BEARER.finditer(item):
                add(match[0])
            # Error strings can contain JSON quoted one or more times.
            candidates = [item]
            for _ in range(3):
                decoded = candidates[-1].replace('\\"', '"').replace("\\'", "'")
                if decoded == candidates[-1]:
                    break
                candidates.append(decoded)
            for candidate in candidates:
                for match in LABELED.finditer(candidate):
                    add(match["value"])
    walk(value)
    return found


def redact_text(text, secrets=()):
    text = str(text)
    for secret in sorted(set(secrets), key=len, reverse=True):
        if not secret or already_masked(secret):
            continue
        replacement = mask(secret)
        if len(secret) <= 4:
            text = re.sub(r"(?<![\w])" + re.escape(secret) + r"(?![\w])", lambda _: replacement, text)
        else:
            text = text.replace(secret, replacement)
        escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
        if escaped != secret:
            text = text.replace(escaped, replacement)
    text = BEARER.sub(lambda m: mask(m[0]), text)
    def hide(match):
        value = clean_secret(match["value"])
        return match[0][:match.start("value") - match.start()] + mask(value)
    text = LABELED.sub(hide, text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{9,}", lambda m: mask(m[0]), text)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)


def redact(value, secrets=None):
    secrets = collect_secrets(value) if secrets is None else secrets
    if isinstance(value, dict):
        return {key: mask(child) if key_name(key) in SECRET_NAMES and not isinstance(child, (dict, list))
                else redact(child, secrets) for key, child in value.items()}
    if isinstance(value, list):
        return [redact(child, secrets) for child in value]
    return redact_text(value, secrets) if isinstance(value, str) else value


def public_record(record, extra_secrets=()):
    secrets = collect_secrets(record) | set(extra_secrets)
    return redact({key: value for key, value in record.items() if key != "raw"}, secrets)


def render(document, analyses=None, extra_secrets=(), group_fingerprints=None, *, allow_truncation=False,
           prepared_groups=None):
    from cloud_log_presentation import render as compact_render
    return compact_render(document, analyses, extra_secrets, group_fingerprints, allow_truncation=allow_truncation,
                          prepared_groups=prepared_groups)
