"""Run the real CLI workflow with synthetic cloud adapters and a local Feishu receiver.

No cloud credentials or network access are used. Not included in the Skill ZIP.
"""
from contextlib import redirect_stdout
import html
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloud_logs

KEY_A = "sk-demo-A-only-synthetic-key-1001"
KEY_B = "sk-demo-B-only-synthetic-key-2002"


def fixtures():
    sources = {"azure-ai": [], "volc-cn": []}
    scenarios = [
        ("azure-ai", "req_timeout_001", KEY_A, "cdance2.0", "UPSTREAM_TIMEOUT", "Upstream read timed out", 504),
        ("volc-cn", "req_timeout_002", KEY_A, "cdance2.0", "UPSTREAM_TIMEOUT", "Timed out waiting for upstream response", 504),
        ("azure-ai", "req_invalid_001", KEY_A, "cdance2.0", "INVALID_PARAMETER", "Invalid value for resolution", 400),
        ("azure-ai", "req_blocked_001", KEY_B, "", "AUTH_KEY_BLOCKED", "blocked", 403),
        ("volc-cn", "req_blocked_002", KEY_B, "", "AUTH_KEY_BLOCKED", "blocked", 403),
    ]
    for index, (source, request_id, key, model, code, message, status) in enumerate(scenarios):
        shared = {"request_id": request_id, "authorization": key, "model": model,
                  "method": "POST", "path": "/v6/contents/generations/tasks"}
        events = [{**shared, "level": "INFO", "event_type": "REQUEST_RECEIVED"}]
        if code == "AUTH_KEY_BLOCKED":
            events.append({**shared, "level": "WARNING" if source == "azure-ai" else "INFO",
                           "logger": "api.auth_key_policy:check", "internal_code": code, "reason": message})
        else:
            events.append({**shared, "level": "INFO", "event_type": "VIDEO_CREATE_TIMING",
                           "upstream_status": status,
                           "upstream_error": {"code": code, "message": message}})
        events.append({**shared, "level": "WARNING", "event_type": "REQUEST_COMPLETED",
                       "gateway_status": status, "gateway_code": "OK"})
        for offset, payload in enumerate(events):
            sources[source].append({"TimeGenerated": f"2026-10-19T02:0{index}:{offset:02d}Z",
                                    "LogMessage": payload})
    return sources


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def preview_html(payloads, result):
    body = []
    for payload in payloads:
        card = payload["card"]
        elements = []
        for item in card["elements"]:
            if item["tag"] == "hr":
                elements.append("<hr>")
            elif "text" in item:
                text = item["text"]["content"]
                if item["text"]["tag"] == "lark_md" and text.startswith("**") and text.endswith("**"):
                    elements.append("<h2>" + html.escape(text[2:-2]) + "</h2>")
                else:
                    elements.append("<pre>" + html.escape(text) + "</pre>")
        body.append('<article><header>' + html.escape(card["header"]["title"]["content"]) +
                    '</header><div class="content">' + "".join(elements) + '</div></article>')
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>飞书合并卡片 · 本地流程预览</title><style>
body{margin:0;background:#f3f5f8;color:#1f2329;font:15px/1.65 "Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:880px;margin:36px auto;padding:0 24px}h1{font-size:25px;margin-bottom:8px}
.note{color:#646a73;margin-bottom:24px}article{background:white;border:1px solid #dfe3e8;border-radius:12px;overflow:hidden;margin-bottom:24px}
header{background:#fff0dc;color:#8a4100;padding:16px 22px;font-weight:600}.content{padding:4px 22px 20px}
pre{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere;margin:14px 0}h2{font-size:17px;margin:20px 0 12px}
hr{border:0;border-top:1px solid #e5e6eb;margin:22px 0}.stats{display:flex;gap:10px;flex-wrap:wrap;margin:20px 0}
.stats span{background:#e9f2ff;color:#2557a7;border-radius:6px;padding:6px 12px}
</style><main><h1>飞书合并卡片 · 本地流程预览</h1>
<p class="note">模拟日志；云查询和飞书接收均在本地替代。下方内容来自实际 send 生成的卡片 JSON，样式为本地预览。</p>''' + \
        '<div class="stats">' + ''.join('<span>' + html.escape(str(value)) + '</span>' for value in (
            f'{result["alert_count"]} 条异常日志', f'{result["trace_tasks"]} 次请求追踪',
            f'{result["primary_groups"]} 个 Key / 模型组', f'{result["error_groups"]} 个错误组',
            f'{result["send"]["sent_messages"]} 张卡片')) + '</div>' + "".join(body) + '</main></html>'


def run_demo(output):
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    state = root / "state"
    profile = Path(__file__).resolve().parents[2] / "assets/profiles/gateway.json"
    config = {"schema_version": 1, "output_timezone": "+08:00", "connections": [
        {"id": "azure-ai", "provider": "azure_log_analytics", "profile": str(profile),
         "service": "anspire-gateway", "environment": ".ai"},
        {"id": "volc-cn", "provider": "volcengine_tls", "topic_ids": ["topic-demo"],
         "profile": str(profile), "service": "anspire-gateway", "environment": ".cn"}],
        "notifications": {"demo": {"provider": "feishu"}}}
    write(root / "config.json", config)
    raw = fixtures()
    calls, payloads, steps = [], [], []

    def resource(connection):
        return "topic-demo" if connection["provider"] == "volcengine_tls" else "workspace-demo"

    def query(connection, start, end, count, term=None, context=False):
        calls.append({"connection": connection["id"], "term": term, "all_levels": context})
        rows = [row for row in raw[connection["id"]] if not term or row["LogMessage"]["request_id"] == term]
        return {"rows": rows, "complete": True, "resource": resource(connection)}

    def receive(request, timeout):
        payloads.append(json.loads(request.data))
        return io.StringIO('{"code":0}')

    def command(arguments, name):
        output_path = root / name
        with patch.object(sys, "argv", ["cloud_logs.py", *arguments, "--output", str(output_path)]), \
                redirect_stdout(io.StringIO()):
            code = cloud_logs.main()
        if code != 0:
            raise AssertionError(f"{arguments[0]} exited {code}")
        steps.append({"command": arguments[0], "exit_code": code, "output": name})
        return output_path.read_text(encoding="utf-8") if arguments[0] == "render" else json.loads(output_path.read_text(encoding="utf-8"))

    env = {name: os.environ[name] for name in ("TEMP", "TMP", "SystemRoot") if name in os.environ}
    env["FEISHU_WEBHOOK"] = "https://open.feishu.cn/open-apis/bot/v2/hook/local-demo"
    adapter = SimpleNamespace(query=query, resource_id=resource)
    with patch.dict(os.environ, env, clear=True), patch("cloud_logs.adapter", return_value=adapter), \
            patch("cloud_log_delivery.urlopen", side_effect=receive), patch("cloud_log_delivery.time.sleep"):
        base = ["--config", str(root / "config.json"), "--state-dir", str(state)]
        scan = command(["scan", *base, "--start", "2026-10-19T01:59:00Z", "--end", "2026-10-19T02:10:00Z"], "01-scan.json")
        tasks = scan["investigation"]["next_actions"]
        assert len(scan["alerts"]) == 6 and len(tasks) == 5
        contexts = []
        for index, task in enumerate(tasks):
            name = f"02-trace-{index + 1}.json"
            command([*task["argv"], "--config", str(root / "config.json")], name)
            contexts.extend(["--context", str(root / name)])
        enriched = command(["enrich", "--input", str(root / "01-scan.json"), *contexts], "03-enriched.json")
        assert enriched["investigation"]["next_actions"] == []
        assert all(row["error_message"] for row in enriched["alerts"])
        associations = {event_id: association for association in enriched["associations"] for event_id in association["event_ids"]}
        analyses = {}
        # Explicit Agent-authored example diagnoses, not script-generated semantic inference.
        diagnoses = {
            "UPSTREAM_TIMEOUT": ("timeout", "upstream_read_timeout", "upstream", "上游响应超时",
                "日志明确记录上游读取超时；尚不能确定是上游负载还是网络原因。", "检查上游响应耗时和服务状态，再判断是否需要调整超时或重试策略。"),
            "INVALID_PARAMETER": ("invalid_request", "invalid_parameter_value", "resolution", "resolution 参数值无效",
                "上游明确拒绝 resolution 参数值；当前证据未提供允许的取值范围。", "核对当前模型支持的 resolution 取值并修正请求。"),
            "AUTH_KEY_BLOCKED": ("permission", "credential_blocked", "authorization_key", "授权 Key 被策略拦截",
                "授权 Key 被访问策略拦截；当前证据未说明具体封禁规则。", "检查该 Key 的启用状态及封禁规则。"),
        }
        for row in enriched["alerts"]:
            category, reason, subject, summary, judgment, recommendation = diagnoses[row["error_code"]]
            association = associations[row["event_id"]]
            evidence = association.get("field_sources", {}).get("error_message", [row["event_id"]])
            analyses[row["event_id"]] = {"certainty": "confirmed", "judgment": judgment, "recommendation": recommendation,
                "classification": {"category": category, "reason": reason, "subject": subject, "summary": summary,
                    "evidence_event_ids": evidence, "evidence_quote": row["error_message"]}}
        write(root / "04-analysis.json", analyses)
        options = ["--input", str(root / "03-enriched.json"), "--analysis-file", str(root / "04-analysis.json")]
        preview = command(["render", *base, *options], "05-preview.txt")
        sent = command(["send", *base, *options, "--channel", "demo"], "06-send.json")
        report = command(["report", "--state-dir", str(state), "--report-id", sent["report_id"]], "07-report.json")
        empty = command(["claim", *base], "08-claim.json")
    assert len(calls) == 7 and all(call["all_levels"] for call in calls if call["term"])
    assert len(report["groups"]) == 3 and len({group["primary_id"] for group in report["groups"]}) == 2
    assert preview.count("分析：") == 3 and preview.count("AUTH_KEY_BLOCKED") == 1
    blocked = preview.split("AUTH_KEY_BLOCKED", 1)[1]
    assert "request_id（2 个）：" in blocked
    assert set(line for line in blocked.splitlines() if line.startswith("req_")) == {"req_blocked_001", "req_blocked_002"}
    assert sent["acknowledged_events"] == 6 and sent["sent_messages"] == 1
    assert sent["analysis_issues"] == {} and sent["classification_issues"] == {}
    assert empty["available_count"] == 0 and empty["pending_count"] == 0
    db = sqlite3.connect(state / "outbox.sqlite3")
    try:
        receipts = db.execute("SELECT COUNT(*) FROM target_receipts").fetchone()[0]
    finally:
        db.close()
    assert receipts == 6
    published = preview + json.dumps(payloads) + json.dumps(report)
    assert KEY_A not in published and KEY_B not in published
    for path in state.rglob("*"):
        if path.is_file():
            assert KEY_A.encode() not in path.read_bytes() and KEY_B.encode() not in path.read_bytes()
    assert all(label not in published for label in ("请求人：", "追踪状态：", "请求结果："))
    write(root / "05-preview.cards.json", payloads)
    result = {"network_mode": "all cloud queries and Feishu responses mocked locally", "steps": steps,
              "query_calls": calls, "alert_count": 6, "trace_tasks": len(tasks), "primary_groups": 2,
              "error_groups": 3, "receipt_count": receipts, "send": sent, "checks_passed": True}
    write(root / "result.json", result)
    (root / "preview.html").write_text(preview_html(payloads, result), encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(run_demo(sys.argv[1]), ensure_ascii=False, indent=2))
