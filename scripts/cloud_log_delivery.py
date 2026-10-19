"""Feishu outgoing webhook transport. Called only by the explicit send command."""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import re
import time
from copy import deepcopy
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from cloud_log_common import LogError, digest, env_setting
from cloud_log_reporting import redact_text, render
from cloud_log_state import (begin_send, load_authorization_fingerprints,
                             pending_for_target, receipt, renew_send)


SECTION_HEADINGS = {"【具体错误】", "【错误分析】", "【查询与追踪说明】"}


def channel_settings(channel):
    if not isinstance(channel, dict) or channel.get("provider") != "feishu":
        raise LogError("当前仅实现飞书通知渠道")
    webhook = env_setting(channel, "webhook_env", "FEISHU_WEBHOOK")
    default_secret = env_setting(channel, "secret_env", "FEISHU_SECRET", required=False)
    if webhook.startswith(("[", "{")):
        try:
            entries = json.loads(webhook)
        except ValueError:
            raise LogError("飞书 Webhook JSON 格式错误") from None
        entries = entries if isinstance(entries, list) else [entries]
    else:
        entries = [webhook]
    if not entries:
        raise LogError("飞书机器人列表不能为空")
    targets, seen = [], set()
    for entry in entries:
        secret = default_secret
        if isinstance(entry, dict):
            webhook = entry.get("webhook")
            secret = entry.get("secret", secret) or ""
        else:
            webhook = entry
        if not isinstance(webhook, str) or not isinstance(secret, str):
            raise LogError("飞书 Webhook 和签名密钥需要字符串")
        parsed = urlparse(webhook.strip())
        prefix = "/open-apis/bot/v2/hook/"
        if (parsed.scheme != "https" or parsed.hostname != "open.feishu.cn" or
                not parsed.path.startswith(prefix) or not parsed.path[len(prefix):] or parsed.username or parsed.password):
            raise LogError("FEISHU_WEBHOOK 需要飞书自定义机器人的 HTTPS Webhook")
        if webhook.strip() in seen:
            raise LogError("飞书配置中同一个 Webhook 重复，需去除重复项")
        seen.add(webhook.strip())
        targets.append({"webhook": webhook.strip(), "secret": secret.strip(), "id": digest(webhook.strip())})
    from cloud_log_notify_state import settings
    settings(channel)
    timeout = channel.get("timeout_seconds", 30)
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise LogError("飞书 timeout_seconds 需要 1 到 300 的整数")
    return targets, env_setting(channel, "keyword_env", "FEISHU_KEYWORD", required=False), timeout


def plain_elements(text):
    """Keep each text element small without cutting Unicode or dropping content."""
    elements, part, size = [], [], 0
    for char in text:
        width = len(char.encode("utf-8"))
        if size + width > 2800:
            elements.append({"tag": "div", "text": {"tag": "plain_text", "content": "".join(part)}})
            part, size = [], 0
        part.append(char)
        size += width
    if part:
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content": "".join(part)}})
    return elements


def card_payload(title, body, keyword, color="orange"):
    elements = []
    for paragraph in body.split("\n\n"):
        if paragraph == "---":
            if elements and elements[-1].get("tag") != "hr":
                elements.append({"tag": "hr"})
            continue
        heading, separator, rest = paragraph.partition("\n")
        if heading.startswith("日志告警｜") and not separator:
            # Service/environment names are data: escape markup and mentions.
            safe_heading = re.sub(r"([\\`*_\[\]])", r"\\\1", html.escape(heading, quote=False))
            if len(safe_heading.encode("utf-8")) <= 2700:
                elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**" + safe_heading + "**"}})
            else:
                elements.extend(plain_elements(heading))
            continue
        if heading in SECTION_HEADINGS:
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**" + heading[1:-1] + "**"}})
            paragraph = rest if separator else ""
        elements.extend(plain_elements(paragraph))
    # Keep keyword verification without a separate line above the first event.
    title = title if not keyword or keyword in title else keyword + "｜" + title
    return {"msg_type": "interactive", "card": {
        "config": {"wide_screen_mode": True},
        "header": {"template": color, "title": {"tag": "plain_text", "content": title}},
        "elements": elements or plain_elements("未记录")}}


def cards(text, keyword):
    """Combine all sources in a batch; split only at card capacity limits."""
    text = text.strip()
    first, _, rest = text.partition("\n")
    single = "\n\n---\n\n" not in text
    title = first if single and first.startswith("日志告警｜") and len(first.encode("utf-8")) <= 300 else "多云日志告警汇总"
    remaining = rest.lstrip("\n") if title == first else text
    color = "red" if any(line in {"日志级别：ERROR", "日志级别：CRITICAL", "日志级别：FATAL"}
                         for line in text.splitlines()) else "orange"
    result = []
    while remaining:
        low, high, best = 1, len(remaining), 0
        while low <= high:
            middle = (low + high) // 2
            payload = card_payload(title, remaining[:middle], keyword, color)
            if (len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= 18000 and
                    len(payload["card"]["elements"]) <= 40):
                best, low = middle, middle + 1
            else:
                high = middle - 1
        if best == 0:
            raise LogError("飞书关键词过长，无法生成卡片")
        if best < len(remaining):
            event_boundary = remaining.rfind("\n\n---\n\n", 0, best)
            line_boundary = remaining.rfind("\n", 0, best)
            if event_boundary > best // 2:
                best = event_boundary
            elif line_boundary > best // 2:
                best = line_boundary + 1
        result.append(card_payload(title, remaining[:best], keyword, color))
        remaining = remaining[best:]
    if not result:
        result.append(card_payload(title, "未记录", keyword, color))
    if len(result) > 1:
        for index, payload in enumerate(result, 1):
            payload["card"]["header"]["title"]["content"] += f" ({index}/{len(result)})"
    return result


def post(webhook, secret, payload, timeout):
    payload = dict(payload)
    if secret:
        timestamp = str(int(time.time()))
        signature = hmac.new((timestamp + "\n" + secret).encode(), b"", hashlib.sha256).digest()
        payload.update(timestamp=timestamp, sign=base64.b64encode(signature).decode())
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        request = Request(webhook, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise LogError(f"飞书 HTTP {exc.code}") from None
    except (URLError, TimeoutError, ValueError):
        raise LogError("飞书网络或响应异常，无法确认投递结果") from None
    code = result.get("code", result.get("StatusCode")) if isinstance(result, dict) else None
    if type(code) is not int or code != 0:
        label = str(code) if isinstance(code, int) else "未知"
        raise LogError("飞书未确认成功，错误码 " + label)


def send_document(config, channel_id, document, analyses, directory=None, secrets=()):
    from pathlib import Path
    from cloud_log_grouping import notification_groups
    from cloud_log_notify_state import (active_plans, confirm_part, create_plan, finish_delivery,
                                       save_report, select_due, settings, snapshots)
    from cloud_log_reporting import collect_secrets
    from cloud_log_workflow import plan_investigation
    investigation = plan_investigation(document)
    if investigation["next_actions"]:
        raise LogError("存在尚未执行的追踪任务；先执行 investigation.next_actions，再用 enrich 合并结果（包括空结果或查询失败）。预览请用 render")
    channel = config.get("notifications", {}).get(channel_id)
    if not channel:
        raise LogError("通知渠道不存在：" + channel_id)
    policy = settings(channel)
    records = document.get("alerts", document.get("records", []))
    diagnostics = document.get("complete") is False or any(
        source.get("error") or source.get("status") == "failed" or source.get("complete") is False
        for source in document.get("sources", []))
    if not records and not diagnostics:
        warnings = [redact_text(str(warning), secrets) for source in document.get("sources", [])
                    for warning in source.get("warnings", [])]
        return {"channel": channel_id, "sent_messages": 0, "reason": "no_findings",
                "warnings_not_sent": list(dict.fromkeys(warnings))}
    targets, keyword, timeout = channel_settings(channel)
    secrets = set(secrets) | {t[key] for t in targets for key in ("webhook", "secret") if t[key]}
    secrets |= collect_secrets(document) | collect_secrets(analyses)
    keyword = redact_text(keyword, secrets)
    delivery_id = document.get("delivery_id")
    if document.get("kind") == "scan" and records and not delivery_id:
        raise LogError("巡检事件缺少 delivery_id，不能跳过投递确认流程")
    if delivery_id and not directory:
        raise LogError("投递巡检批次需要对应的状态目录")
    if directory and not Path(directory).is_absolute():
        raise LogError("state-dir 必须是固定的绝对路径")
    fingerprints = load_authorization_fingerprints(directory, [r["event_id"] for r in records]) if delivery_id else {}
    groups = notification_groups(document, analyses, fingerprints)
    analyses = {**document.get("notification_analyses", {}), **(analyses or {})}
    report_id = save_report(directory, document, analyses, groups, secrets) if directory else None
    document = deepcopy(document)
    if report_id:
        document["notification_report_id"] = report_id
    if delivery_id:
        begin_send(directory, delivery_id, [r["event_id"] for r in records], digest(["channel", channel_id]),
                   previous_destinations=[digest([channel_id, t["webhook"]]) for t in targets])
        try:
            snapshots(directory, delivery_id, groups, secrets)
        except Exception:
            receipt(directory, delivery_id, False)
            raise
    states = {item["event_id"]: item for item in investigation["events"]}
    for group in groups:
        group["cooldown_eligible"] = all(
            states[row["event_id"]]["trace_status"] in {"complete", "not_required"}
            and states[row["event_id"]]["error_evidence_status"] != "conflicting"
            for row in group["records"])
    sent, results, failures, reports_used = 0, [], [], []
    for index, target in enumerate(targets, 1):
        target_sent = 0
        try:
            pending = pending_for_target(directory, [r["event_id"] for r in records], target["id"]) if delivery_id else {r["event_id"] for r in records}
            if delivery_id and not pending:
                results.append({"target": index, "status": "already_delivered", "sent_messages": 0})
                continue
            plans = active_plans(directory, target["id"], pending) if delivery_id else []
            frozen_ids = {event_id for plan in plans for event_id in plan["event_ids"]}
            target_groups = []
            for group in groups:
                rows = [row for row in group["records"] if row["event_id"] in pending - frozen_ids]
                if rows:
                    target_groups.append({**group, "records": rows})
            due, delayed = (select_due(directory, target["id"], target_groups, policy, diagnostics)
                            if delivery_id else (target_groups, []))
            if due or (diagnostics and not plans):
                target_document = deepcopy(document)
                included = {row["event_id"] for group in due for row in group["records"]}
                target_document["alerts" if "alerts" in document else "records"] = [r for r in records if r["event_id"] in included]
                target_document["associations"] = [g for g in document.get("associations", []) if set(g.get("event_ids", [])) & included]
                parts = cards(render(target_document, analyses, secrets, fingerprints,
                                     allow_truncation=bool(report_id)), keyword)
                plans.append(create_plan(directory, target["id"], due, parts, report_id) if delivery_id and due else
                             {"parts": parts, "next_part": 0, "event_ids": [], "report_id": report_id})
            for plan in plans:
                if plan.get("report_id"):
                    reports_used.append(plan["report_id"])
                for part_index in range(plan["next_part"], len(plan["parts"])):
                    if delivery_id:
                        renew_send(directory, delivery_id, len(records))
                    post(target["webhook"], target["secret"], plan["parts"][part_index], timeout)
                    target_sent += 1
                    sent += 1
                    if delivery_id and plan.get("id"):
                        confirm_part(directory, delivery_id, target["id"], plan, part_index)
                    if part_index + 1 < len(plan["parts"]):
                        time.sleep(0.7)
            results.append({"target": index, "status": "deferred" if delayed and not target_sent else
                            "delivered_with_deferred" if delayed else "delivered",
                            "sent_messages": target_sent, "deferred_events": len(delayed)})
        except Exception as exc:
            reason = redact_text(str(exc), secrets) if isinstance(exc, LogError) else type(exc).__name__
            failures.append(f"目标 {index} 已确认 {target_sent} 段：{reason}")
    if failures:
        if delivery_id:
            receipt(directory, delivery_id, False)
        raise LogError(f"飞书投递未完成，{len(results)}/{len(targets)} 个目标完成；" + "；".join(failures) +
                       "。重新 claim 后继续已保存的卡片分段；网络结果不确定的片段仍可能重复")
    completion = (finish_delivery(directory, delivery_id, [target["id"] for target in targets]) if delivery_id else
                  {"acknowledged_events": 0, "deferred_events": 0, "next_notification_at": None})
    return {"channel": channel_id, "message_format": "interactive", "sent_messages": sent,
            "delivery_id": delivery_id, **completion, "targets": results,
            "report_id": reports_used[0] if reports_used else report_id,
            "report_ids": list(dict.fromkeys(reports_used or ([report_id] if report_id else []))),
            "classification_issues": {event_id: issue for group in groups for event_id, issue in group["classification_issues"].items()}}
