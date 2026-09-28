import logging
import os
import json
import time
import requests
from datetime import datetime, timezone
from flask import Request, make_response, jsonify
import zcatalyst_sdk

logger = logging.getLogger()

ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"


def _desk_base():
    domain = os.environ.get("ZOHO_DESK_DOMAIN", "desk.zoho.com")
    return f"https://{domain}/api/v1"


def _get_access_token():
    resp = requests.post(ZOHO_TOKEN_URL, data={
        "refresh_token": os.environ.get("ZOHO_REFRESH_TOKEN"),
        "client_id": os.environ.get("ZOHO_CLIENT_ID"),
        "client_secret": os.environ.get("ZOHO_CLIENT_SECRET"),
        "grant_type": "refresh_token",
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def _desk_headers():
    return {
        "Authorization": f"Zoho-oauthtoken {_get_access_token()}",
        "orgId": os.environ.get("ZOHO_ORG_ID"),
    }


def _fetch_violations():
    headers = _desk_headers()
    base = _desk_base()

    resp = requests.get(
        f"{base}/tickets",
        headers=headers,
        params={"limit": 20, "sortBy": "-createdTime", "status": "open"},
    )
    resp.raise_for_status()
    ticket_list = resp.json().get("data", [])

    violated = []
    for t in ticket_list:
        detail = requests.get(f"{base}/tickets/{t['id']}", headers=headers)
        if detail.ok:
            full = detail.json()
            if full.get("isOverDue") or full.get("isResponseOverDue"):
                violated.append(full)

    return violated


def _compute_metrics(violations):
    if not violations:
        return {"total": 0, "by_category": {}, "by_agent": {}, "by_priority": {}}

    by_category = {}
    by_agent = {}
    by_priority = {}

    for t in violations:
        cat = t.get("category") or "Unknown"
        priority = t.get("priority") or "Unknown"
        assignee = t.get("assignee") or {}
        agent = assignee.get("firstName", "Unassigned") if assignee else "Unassigned"

        by_category[cat] = by_category.get(cat, 0) + 1
        by_agent[agent] = by_agent.get(agent, 0) + 1
        by_priority[priority] = by_priority.get(priority, 0) + 1

    return {
        "total": len(violations),
        "by_category": by_category,
        "by_agent": by_agent,
        "by_priority": by_priority,
    }


def _risk_score(ticket):
    priority_weights = {"urgent": 1.3, "high": 1.15, "medium": 1.0, "low": 0.85}
    due_str = ticket.get("dueDate")
    created_str = ticket.get("createdTime")

    priority = (ticket.get("priority") or "medium").lower()
    weight = priority_weights.get(priority, 1.0)

    if not due_str or not created_str:
        return round(weight * 35, 1)

    try:
        due = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
        created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
    except Exception:
        return round(weight * 35, 1)

    now = datetime.now(timezone.utc)
    if due <= now:
        return None  # já vencido — aparece na aba de violações

    total_sla = (due - created).total_seconds()
    if total_sla <= 0:
        return 0.0

    elapsed = (now - created).total_seconds()
    pct = (elapsed / total_sla) * 100
    return round(min(100.0, pct * weight), 1)


def _fetch_at_risk_tickets():
    headers = _desk_headers()
    base = _desk_base()

    resp = requests.get(
        f"{base}/tickets",
        headers=headers,
        params={"limit": 50, "sortBy": "-createdTime", "status": "open"},
    )
    resp.raise_for_status()
    ticket_list = resp.json().get("data", [])

    at_risk = []
    for t in ticket_list:
        detail = requests.get(f"{base}/tickets/{t['id']}", headers=headers)
        if not detail.ok:
            continue
        full = detail.json()

        if full.get("isOverDue") or full.get("isResponseOverDue"):
            continue

        score = _risk_score(full)
        if score is None:
            continue

        full["_risk_score"] = score
        full["_risk_level"] = "alto" if score >= 75 else "medio" if score >= 45 else "baixo"

        due_str = full.get("dueDate")
        if due_str:
            try:
                due = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
                remaining = (due - datetime.now(timezone.utc)).total_seconds()
                h = int(remaining // 3600)
                m = int((remaining % 3600) // 60)
                full["_time_remaining"] = f"{h}h {m}min" if h > 0 else f"{m}min"
            except Exception:
                full["_time_remaining"] = "–"
        else:
            full["_time_remaining"] = "–"

        at_risk.append(full)

    at_risk.sort(key=lambda x: x["_risk_score"], reverse=True)
    return at_risk


def _analyze_with_ai(violations):
    summaries = [
        {
            "id": t.get("ticketNumber"),
            "subject": t.get("subject"),
            "category": t.get("category"),
            "priority": t.get("priority"),
            "description": (t.get("description") or "")[:400],
            "created": t.get("createdTime"),
        }
        for t in violations[:25]
    ]

    prompt = (
        "Analise os seguintes tickets de suporte que violaram o SLA e identifique padrões.\n\n"
        f"Tickets:\n{json.dumps(summaries, ensure_ascii=False, indent=2)}\n\n"
        "Responda APENAS em JSON válido com esta estrutura exata:\n"
        "{\n"
        '  "recurring_issues": ["descrição do problema recorrente"],\n'
        '  "product_defects": ["produto ou componente com possível defeito"],\n'
        '  "delay_patterns": ["padrão que explica os atrasos"],\n'
        '  "recommendations": ["ação recomendada"]\n'
        "}"
    )

    api_key = os.environ.get("GEMINI_API_KEY")
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }

    for attempt in range(3):
        resp = requests.post(url, json=payload, headers={"x-goog-api-key": api_key}, timeout=30)
        if resp.status_code == 503:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    resp.raise_for_status()


CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def handler(request: Request):
    zcatalyst_sdk.initialize()

    if request.method == "OPTIONS":
        return make_response("", 204, CORS)

    path = request.path
    method = request.method

    try:
        if path in ("/", "/health"):
            return make_response(jsonify({"status": "ok"}), 200, CORS)

        if path == "/debug/models" and method == "GET":
            api_key = os.environ.get("GEMINI_API_KEY")
            # Test generateContent with a minimal prompt to diagnose auth
            test_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
            test_payload = {"contents": [{"parts": [{"text": "say hi"}]}]}
            r1 = requests.post(f"{test_url}?key={api_key}", json=test_payload)
            r2 = requests.post(test_url, json=test_payload, headers={"x-goog-api-key": api_key})
            return make_response(jsonify({
                "key_prefix": api_key[:6] if api_key else None,
                "with_query_param": {"status": r1.status_code, "body": r1.json()},
                "with_header": {"status": r2.status_code, "body": r2.json()},
            }), 200, CORS)

        if path == "/debug/tickets" and method == "GET":
            headers = _desk_headers()
            resp = requests.get(
                f"{_desk_base()}/tickets",
                headers=headers,
                params={"limit": 5, "sortBy": "-createdTime", "status": "open"},
            )
            tickets = resp.json().get("data", [])
            # Return first ticket from list as-is (no simplification)
            return make_response(jsonify(tickets[0] if tickets else {}), 200, CORS)

        if path == "/violations" and method == "GET":
            data = _fetch_violations()
            return make_response(jsonify({"data": data, "count": len(data)}), 200, CORS)

        if path == "/metrics" and method == "GET":
            violations = _fetch_violations()
            return make_response(jsonify(_compute_metrics(violations)), 200, CORS)

        if path == "/predict" and method == "GET":
            data = _fetch_at_risk_tickets()
            counts = {"alto": 0, "medio": 0, "baixo": 0}
            for t in data:
                counts[t.get("_risk_level", "baixo")] += 1
            return make_response(jsonify({"data": data, "count": len(data), "by_risk": counts}), 200, CORS)

        if path == "/analyze" and method == "POST":
            body = request.get_json(silent=True) or {}
            violations = body.get("violations") or _fetch_violations()
            try:
                insights = _analyze_with_ai(violations)
            except Exception as ai_exc:
                import traceback
                logger.error("AI analysis error: %s\n%s", str(ai_exc), traceback.format_exc())
                return make_response(jsonify({"error": str(ai_exc), "type": type(ai_exc).__name__}), 500, CORS)
            return make_response(jsonify(insights), 200, CORS)

        return make_response(jsonify({"error": "Not found"}), 404, CORS)

    except Exception as exc:
        logger.exception("Handler error")
        return make_response(jsonify({"error": str(exc)}), 500, CORS)
