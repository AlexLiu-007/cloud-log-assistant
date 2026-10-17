"""Azure Log Analytics REST adapter, derived from the existing monitor client."""
from __future__ import annotations

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from cloud_log_common import LogError, credential_status, custom_query, env_setting

ENV = {"tenant_id_env": "AZURE_LOG_TENANT_ID", "client_id_env": "AZURE_LOG_CLIENT_ID",
       "client_secret_env": "AZURE_LOG_CLIENT_SECRET", "workspace_id_env": "LOG_ANALYTICS_WORKSPACE_ID"}


def configuration_status(connection):
    return {"environment": credential_status(connection, ENV), "dependency": "Python standard library"}


def post(url, body, headers, timeout):
    if urlparse(url).scheme != "https":
        raise LogError("Azure 接口必须使用 HTTPS")
    try:
        request = Request(url, data=body, headers=headers, method="POST")
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        raise LogError(f"Azure HTTP {exc.code}；检查身份权限、资源配置或查询语法") from None
    except URLError:
        raise LogError("Azure 网络请求失败") from None


def identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise LogError("Azure 表名和字段名需要简单标识符；复杂表达式请使用 query_file")
    return value


def query(connection, start, end, limit, term=None, context=False):
    tenant, client, secret, workspace = [env_setting(connection, key, name) for key, name in ENV.items()]
    timeout = int(connection.get("timeout_seconds", 60))
    authority = connection.get("authority_host", "https://login.microsoftonline.com").rstrip("/")
    endpoint = connection.get("api_endpoint", "https://api.loganalytics.azure.com").rstrip("/")
    token = post(authority + "/" + quote(tenant, safe="") + "/oauth2/v2.0/token",
                 urlencode({"client_id": client, "client_secret": secret, "grant_type": "client_credentials",
                            "scope": connection.get("scope", "https://api.loganalytics.io/.default")}).encode(),
                 {"Content-Type": "application/x-www-form-urlencoded"}, min(timeout, 30))
    if not token.get("access_token"):
        raise LogError("Azure 认证响应未包含 access_token")
    # Trace uses the ordinary source query rather than a narrowed custom anomaly query.
    query_connection = {k: v for k, v in connection.items() if k != "query_file"} if context else connection
    sql = custom_query(query_connection, start, end, limit + 1, term)
    custom = sql is not None
    if sql is None:
        table = identifier(connection.get("table", "ContainerLogV2"))
        timestamp = identifier(connection.get("time_column", "TimeGenerated"))
        content = identifier(connection.get("content_column", "LogMessage"))
        container = identifier(connection.get("container_column", "ContainerName"))
        clauses = [table,
                   f"| where {timestamp} >= datetime({start.isoformat()}) and {timestamp} < datetime({end.isoformat()})"]
        if connection.get("containers"):
            values = ", ".join(json.dumps(x) for x in connection["containers"])
            clauses.append(f"| where {container} in ({values})")
        clauses.append(f"| extend FullLog=tostring({content}), TimeGenerated={timestamp}")
        if term:
            search_columns = connection.get("search_columns", ["FullLog", "RequestId", "trace_id", "TraceId", "OperationId"])
            expressions = ["tostring(column_ifexists(" + json.dumps(identifier(column)) + ', "")) contains '
                           + json.dumps(term, ensure_ascii=False) for column in search_columns]
            clauses.append("| where " + " or ".join(expressions))
        clauses.extend(["| order by TimeGenerated asc", f"| take {limit + 1}"])
        sql = "\n".join(clauses)
    payload = post(endpoint + "/v1/workspaces/" + quote(workspace, safe="") + "/query",
                   json.dumps({"query": sql, "timespan": start.isoformat() + "/" + end.isoformat()}).encode(),
                   {"Authorization": "Bearer " + token["access_token"], "Content-Type": "application/json"}, timeout)
    warnings, rows = [], []
    complete = not bool(payload.get("error"))
    if not complete:
        warnings.append("Azure 返回部分结果；不能据此断言没有其他日志")
    tables = payload.get("tables")
    if not isinstance(tables, list):
        raise LogError("Azure 响应缺少 tables")
    if not any(isinstance(table, dict) and table.get("name", "PrimaryResult") == "PrimaryResult" for table in tables):
        raise LogError("Azure 响应缺少 PrimaryResult，不能认定查询无结果")
    for table in tables:
        if table.get("name", "PrimaryResult") != "PrimaryResult":
            continue
        columns = [column["name"] for column in table.get("columns", [])]
        for values in table.get("rows", []):
            if len(values) != len(columns):
                raise LogError("Azure 查询列数与行数据不匹配")
            rows.append(dict(zip(columns, values)))
    if len(rows) > limit:
        complete = False
        warnings.append("Azure 达到 max_rows，已保留部分记录；缩小范围或提高配置后继续查询")
    if custom:
        warnings.append("自定义 KQL 的聚合、筛选及 LIMIT 由查询文件决定，不代表完整原始日志")
    return {"rows": rows[:limit], "complete": complete, "warnings": warnings,
            "resource": workspace, "query_kind": "custom" if custom else "raw", "scanned": len(rows)}
