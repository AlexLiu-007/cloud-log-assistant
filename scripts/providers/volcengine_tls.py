"""TLS raw pagination and SQL analysis results, without status-code exclusions."""
from __future__ import annotations

import importlib.util
import json
import logging

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
    return {"environment": credential_status(connection, {k:v for k,v in ENV.items() if k != "topic_id_env"}),
            "topic_count": len(topics),
            "dependency": "volcengine", "dependency_available": importlib.util.find_spec("volcengine") is not None}


def read_batch(result):
    if result.analysis is True:
        analysis = result.analysis_result
        batch = analysis.analysis_data if analysis is not None else None
        kind = "analysis"
    elif result.analysis is False:
        batch, kind = result.logs, "raw"
    else:
        raise LogError("火山 TLS 响应缺少有效的 analysis 标识")
    if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
        raise LogError("火山 TLS 查询结果不是对象数组")
    return batch, kind


def query(connection, start, end, limit, term=None, context=False):
    try:
        from volcengine.tls.TLSService import TLSService
        from volcengine.tls.tls_requests import SearchLogsRequest
    except ImportError:
        raise LogError("缺少 volcengine SDK；使用当前 Python 安装 scripts/requirements.txt") from None
    access = env_setting(connection, "access_key_env", "VOLC_ACCESS_KEY")
    secret = env_setting(connection, "secret_key_env", "VOLC_SECRET_KEY")
    topics = configured_topics(connection)
    if len(topics) != 1:
        raise LogError("多 Topic 需要通过 CLI 展开为独立资源查询")
    topic = topics[0]
    security_token = env_setting(connection, "session_token_env", "VOLC_SESSION_TOKEN", required=False)
    # The SDK logs raw exceptions and initialization messages to stdout.
    # Keep CLI JSON clean and expose only our sanitized diagnostics.
    logging.getLogger("tls-python-sdk-logger").disabled = True
    service = TLSService(endpoint=connection.get("endpoint", "tls-cn-shanghai.volces.com"),
                         region=connection.get("region", "cn-shanghai"), access_key_id=access,
                         access_key_secret=secret, security_token=security_token or None,
                         timeout=int(connection.get("timeout_seconds", 30)))
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
            result = service.search_logs(SearchLogsRequest(
                topic_id=topic, query=sql, start_time=int(start.timestamp() * 1000),
                end_time=int(end.timestamp() * 1000), limit=min(100, limit - scanned),
                context=cursor, sort="asc", highlight=False, must_complete=True)).get_search_result()
            batch, kind = read_batch(result)
        except Exception as exc:
            if not rows:
                raise LogError("火山 TLS 查询失败（" + type(exc).__name__ + "）；检查授权、区域和查询语法") from None
            complete = False
            warnings.append("火山 TLS 后续分页失败，已保留成功获取的记录")
            break
        if result.result_status != "complete":
            complete = False
            warnings.append("火山 TLS 返回未完成结果")
        rows.extend(batch)
        scanned += len(batch)
        if kind == "analysis":
            warnings.append("TLS SQL 结果来自 analysis_data；无原始日志翻页游标，范围受 SQL 自身 LIMIT/聚合约束")
            if len(rows) > limit:
                complete = False
            break
        if result.list_over is True:
            break
        if result.list_over is not False:
            complete = False
            warnings.append("火山 TLS 分页结束标识无效")
            break
        after = result.context
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
