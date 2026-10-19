"""TLS SearchLogs REST API and V4 signing using only the Python standard library."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cloud_log_common import LogError, credential_status, custom_query, env_setting

ENV = {"access_key_env": "VOLC_ACCESS_KEY", "secret_key_env": "VOLC_SECRET_KEY",
       "topic_id_env": "VOLC_LOG_TOPIC_ID"}


def configured_topics(connection):
    if connection.get("_topic_id"):
        return [connection["_topic_id"]]
    if "topic_ids" in connection:
        topics = connection["topic_ids"]
    else:
        key = "topic_ids_env" if "topic_ids_env" in connection else "topic_id_env"
        value = env_setting(connection, key, "VOLC_LOG_TOPIC_ID")
        if value.startswith("["):
            try:
                topics = json.loads(value)
            except ValueError:
                raise LogError("火山 Topic 数组需要有效 JSON") from None
        else:
            topics = [value]
    if (not isinstance(topics, list) or not topics or
            any(not isinstance(t, str) or not t.strip() for t in topics)):
        raise LogError("火山 topic_ids 需要非空字符串数组")
    topics = [t.strip() for t in topics]
    if len(topics) != len(set(topics)):
        raise LogError("火山 Topic 配置有重复项")
    return topics


def expand_connections(connection):
    return [{**connection, "_topic_id": topic} for topic in configured_topics(connection)]


def resource_id(connection):
    topics = configured_topics(connection)
    return topics[0] if len(topics) == 1 else ""


def configuration_status(connection):
    topics = configured_topics(connection)
    endpoint_url(connection)
    return {"environment": credential_status(connection, {k:v for k,v in ENV.items() if k != "topic_id_env"}),
            "topic_count": len(topics),
            "dependency": "Python standard library", "dependency_available": True}


def endpoint_url(connection):
    endpoint = connection.get("endpoint", "tls-cn-shanghai.volces.com")
    if not isinstance(endpoint, str) or re.search(r"\s", endpoint):
        raise LogError("火山 TLS endpoint 需要 HTTPS 主机地址")
    parsed = urlparse(endpoint if "://" in endpoint else "https://" + endpoint)
    try:
        valid_port = parsed.port is None or 1 <= parsed.port <= 65535
    except ValueError:
        valid_port = False
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or
            parsed.query or parsed.fragment or parsed.path not in ("", "/") or not valid_port):
        raise LogError("火山 TLS endpoint 需要 HTTPS 主机地址，不包含路径、查询或用户信息")
    return "https://" + parsed.netloc + "/SearchLogs"


def signed_request(connection, payload, *, now=None):
    """Match SearchLogs 0.2.0 and the official TLS SDK's V4 wire contract."""
    url = endpoint_url(connection)
    region = connection.get("region", "cn-shanghai")
    if not isinstance(region, str) or not re.fullmatch(r"[a-z0-9-]+", region):
        raise LogError("火山 TLS region 配置无效")
    access = env_setting(connection, "access_key_env", "VOLC_ACCESS_KEY")
    secret = env_setting(connection, "secret_key_env", "VOLC_SECRET_KEY")
    token = env_setting(connection, "session_token_env", "VOLC_SESSION_TOKEN", required=False)
    body = json.dumps(payload).encode("utf-8")
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    host = urlparse(url).netloc
    canonical_host = host[:-4] if host.endswith(":443") else host
    digest = hashlib.sha256(body).hexdigest()
    signed = {"content-type": "application/json", "host": canonical_host,
              "x-content-sha256": digest, "x-date": stamp}
    if token:
        signed["x-security-token"] = token
    names = ";".join(sorted(signed))
    canonical_headers = "".join(key + ":" + signed[key] + "\n" for key in sorted(signed))
    canonical = "\n".join(("POST", "/SearchLogs", "", canonical_headers, names, digest))
    scope = "/".join((stamp[:8], region, "TLS", "request"))
    signing_text = "\n".join(("HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()))
    key = secret.encode()
    for value in (stamp[:8], region, "TLS", "request"):
        key = hmac.new(key, value.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, signing_text.encode(), hashlib.sha256).hexdigest()
    headers = {"Host": host, "Content-Type": "application/json",
               "Content-MD5": hashlib.md5(body, usedforsecurity=False).hexdigest(),
               "X-Content-Sha256": digest, "X-Date": stamp, "x-tls-apiversion": "0.2.0",
               "Authorization": "HMAC-SHA256 Credential=" + access + "/" + scope +
                                ", SignedHeaders=" + names + ", Signature=" + signature}
    if token:
        headers["X-Security-Token"] = token
    return Request(url, data=body, headers=headers, method="POST")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def search_logs(connection, payload):
    request = signed_request(connection, payload)
    try:
        with build_opener(_NoRedirect()).open(request, timeout=int(connection.get("timeout_seconds", 30))) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise LogError(f"火山 TLS HTTP {exc.code}；检查权限、区域和查询语法") from None
    except (URLError, TimeoutError, OSError):
        raise LogError("火山 TLS 网络请求失败") from None
    except (ValueError, UnicodeError):
        raise LogError("火山 TLS 响应不是有效 JSON") from None
    if not isinstance(result, dict):
        raise LogError("火山 TLS 响应需要 JSON 对象")
    return result


def read_batch(result):
    if result.get("Analysis") is True:
        analysis = result.get("AnalysisResult")
        batch = analysis.get("Data") if isinstance(analysis, dict) else None
        kind = "analysis"
    elif result.get("Analysis") is False:
        batch, kind = result.get("Logs"), "raw"
    else:
        raise LogError("火山 TLS 响应缺少有效的 analysis 标识")
    if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
        raise LogError("火山 TLS 查询结果不是对象数组")
    return batch, kind


def query(connection, start, end, limit, term=None, context=False):
    topics = configured_topics(connection)
    if len(topics) != 1:
        raise LogError("多 Topic 需要通过 CLI 展开为独立资源查询")
    topic = topics[0]
    query_connection = {k: v for k, v in connection.items() if k != "query_file"} if context else connection
    sql = custom_query(query_connection, start, end, limit, term)
    custom = sql is not None
    if sql is None:
        clauses = []
        if connection.get("containers"):
            clauses.append("(" + " OR ".join("__container_name__:" + json.dumps(x)
                                            for x in connection["containers"]) + ")")
        if term:
            clauses.append(json.dumps(term, ensure_ascii=False))
        sql = " AND ".join(clauses) or "*"
    rows, warnings, cursor, seen = [], [], None, set()
    complete, kind = True, "raw"
    scanned = 0
    max_pages = int(connection.get("max_pages", 100))
    for _ in range(max_pages):
        try:
            payload = {"TopicId": topic, "Query": sql, "StartTime": int(start.timestamp() * 1000),
                       "EndTime": int(end.timestamp() * 1000), "Limit": min(100, limit - scanned),
                       "Sort": "asc", "HighLight": False, "MustComplete": True}
            if cursor is not None:
                payload["Context"] = cursor
            result = search_logs(connection, payload)
            batch, kind = read_batch(result)
        except Exception as exc:
            if not rows:
                if isinstance(exc, LogError):
                    raise
                raise LogError("火山 TLS 查询失败（" + type(exc).__name__ + "）；检查授权、区域和查询语法") from None
            complete = False
            warnings.append("火山 TLS 后续分页失败，已保留成功获取的记录")
            break
        if result.get("ResultStatus") != "complete":
            complete = False
            warnings.append("火山 TLS 返回未完成结果")
        rows.extend(batch)
        scanned += len(batch)
        if kind == "analysis":
            warnings.append("TLS SQL 结果来自 analysis_data；无原始日志翻页游标，范围受 SQL 自身 LIMIT/聚合约束")
            if len(rows) > limit:
                complete = False
            break
        if result.get("ListOver") is True:
            break
        if result.get("ListOver") is not False:
            complete = False
            warnings.append("火山 TLS 分页结束标识无效")
            break
        after = result.get("Context")
        if not isinstance(after, str) or not after or after in seen:
            complete = False
            warnings.append("火山 TLS 后续游标缺失或重复")
            break
        seen.add(after)
        cursor = after
        if scanned >= limit:
            complete = False
            warnings.append("火山 TLS 达到 max_rows，尚有后续日志")
            break
    else:
        complete = False
        warnings.append("火山 TLS 达到 max_pages，尚未完成分页")
    if custom and kind == "raw":
        warnings.append("使用自定义 TLS 查询，结果范围受查询文件的条件约束")
    return {"rows": rows[:limit], "complete": complete, "warnings": warnings,
            "resource": topic, "query_kind": kind, "scanned": scanned}
