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

# In-memory cache for history stats (30 min TTL)
_history_cache = {"agent": {}, "cat": {}, "expires": 0}


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

    by_category, by_agent, by_priority = {}, {}, {}

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
        return None

    total_sla = (due - created).total_seconds()
    if total_sla <= 0:
        return 0.0

    elapsed = (now - created).total_seconds()
    pct = (elapsed / total_sla) * 100
    return round(min(100.0, pct * weight), 1)


# ── Data Store helpers ────────────────────────────────────────────────────────

def _get_history_stats():
    """Read AgentStats and CategoryStats from Catalyst Data Store (cached 30 min)."""
    now = time.time()
    if now < _history_cache["expires"]:
        return _history_cache["agent"], _history_cache["cat"]

    try:
        app = zcatalyst_sdk.initialize()
        agent_raw = app.datastore().table("AgentStats").get_paged_rows(max_rows=200)
        cat_raw = app.datastore().table("CategoryStats").get_paged_rows(max_rows=200)

        agent_rows = (agent_raw.get("data") if isinstance(agent_raw, dict) else agent_raw) or []
        cat_rows = (cat_raw.get("data") if isinstance(cat_raw, dict) else cat_raw) or []

        agent_stats = {}
        for r in agent_rows:
            key = (r.get("agent_name", ""), r.get("category", ""))
            try:
                agent_stats[key] = float(r.get("late_rate", 0))
            except (ValueError, TypeError):
                agent_stats[key] = 0.0

        cat_stats = {}
        for r in cat_rows:
            try:
                cat_stats[r.get("category", "")] = float(r.get("late_rate", 0))
            except (ValueError, TypeError):
                cat_stats[r.get("category", "")] = 0.0

        _history_cache.update({"agent": agent_stats, "cat": cat_stats, "expires": now + 1800})
        logger.info("Data Store: loaded %d agent stats, %d category stats", len(agent_stats), len(cat_stats))
        return agent_stats, cat_stats

    except Exception as exc:
        logger.warning("Data Store read failed (using empty stats): %s", exc)
        return {}, {}


def _risk_score_with_history(ticket, agent_stats, cat_stats):
    """Enhanced risk score using historical late rates from Data Store."""
    base = _risk_score(ticket)
    if base is None:
        return None

    category = (ticket.get("category") or "").strip()
    assignee = ticket.get("assignee") or {}
    agent = (assignee.get("firstName") or "") if assignee else ""

    cat_rate = cat_stats.get(category, 0.0)
    agent_rate = agent_stats.get((agent, category), agent_stats.get((agent, ""), 0.0))

    # History boost: up to +25 points when late rate is 100%
    boost = round((cat_rate * 0.4 + agent_rate * 0.6) * 25, 1)
    return round(min(100.0, base + boost), 1)


def _sync_history():
    """Fetch closed Desk tickets, compute agent/category late rates, save to Data Store."""
    headers = _desk_headers()
    base = _desk_base()

    resp = requests.get(
        f"{base}/tickets",
        headers=headers,
        params={"limit": 50, "status": "Closed", "sortBy": "-closedTime"},
    )
    resp.raise_for_status()
    ticket_list = resp.json().get("data", [])

    agent_acc = {}  # {(agent, category): [total, late]}
    cat_acc = {}    # {category: [total, late]}

    for t in ticket_list:
        detail = requests.get(f"{base}/tickets/{t['id']}", headers=headers)
        if not detail.ok:
            continue
        full = detail.json()

        category = (full.get("category") or "Unknown").strip()
        assignee = full.get("assignee") or {}
        agent = (assignee.get("firstName") or "Unassigned") if assignee else "Unassigned"

        # Determine if ticket was resolved late
        is_late = bool(full.get("isOverDue") or full.get("isResponseOverDue"))
        if not is_late:
            closed_str = full.get("closedTime")
            due_str = full.get("dueDate")
            if closed_str and due_str:
                try:
                    closed = datetime.fromisoformat(closed_str.replace("Z", "+00:00"))
                    due = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
                    is_late = closed > due
                except Exception:
                    pass

        k = (agent, category)
        if k not in agent_acc:
            agent_acc[k] = [0, 0]
        agent_acc[k][0] += 1
        if is_late:
            agent_acc[k][1] += 1

        if category not in cat_acc:
            cat_acc[category] = [0, 0]
        cat_acc[category][0] += 1
        if is_late:
            cat_acc[category][1] += 1

    # Write to Data Store
    app = zcatalyst_sdk.initialize()
    now_str = datetime.now(timezone.utc).isoformat()

    def _upsert_table(table_name, rows_data, key_columns):
        table = app.datastore().table(table_name)
        try:
            raw = table.get_paged_rows(max_rows=200)
            existing = (raw.get("data") if isinstance(raw, dict) else raw) or []
        except Exception:
            existing = []

        # Build index of existing rows by key
        existing_index = {}
        for row in existing:
            key = tuple(str(row.get(c, "")) for c in key_columns)
            existing_index[key] = str(row.get("ROWID", ""))

        for row_data in rows_data:
            key = tuple(str(row_data.get(c, "")) for c in key_columns)
            if key in existing_index:
                row_data["ROWID"] = existing_index[key]
                try:
                    table.update_row(row_data)
                except Exception as e:
                    logger.warning("Update failed, inserting instead: %s", e)
                    table.insert_row({k: v for k, v in row_data.items() if k != "ROWID"})
            else:
                table.insert_row(row_data)

    agent_rows = []
    for (agent, category), (total, late) in agent_acc.items():
        late_rate = round(late / total, 3) if total > 0 else 0.0
        agent_rows.append({
            "agent_name": agent,
            "category": category,
            "total_tickets": total,
            "late_tickets": late,
            "late_rate": late_rate,
            "last_updated": now_str,
        })

    cat_rows = []
    for category, (total, late) in cat_acc.items():
        late_rate = round(late / total, 3) if total > 0 else 0.0
        cat_rows.append({
            "category": category,
            "total_tickets": total,
            "late_tickets": late,
            "late_rate": late_rate,
            "last_updated": now_str,
        })

    _upsert_table("AgentStats", agent_rows, ["agent_name", "category"])
    _upsert_table("CategoryStats", cat_rows, ["category"])

    # Invalidate cache
    _history_cache["expires"] = 0

    return {
        "synced_tickets": len(ticket_list),
        "agent_entries": len(agent_rows),
        "category_entries": len(cat_rows),
        "timestamp": now_str,
    }


# ── Risk predictor ────────────────────────────────────────────────────────────

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

    agent_stats, cat_stats = _get_history_stats()
    has_history = bool(agent_stats or cat_stats)

    at_risk = []
    for t in ticket_list:
        detail = requests.get(f"{base}/tickets/{t['id']}", headers=headers)
        if not detail.ok:
            continue
        full = detail.json()

        if full.get("isOverDue") or full.get("isResponseOverDue"):
            continue

        score = _risk_score_with_history(full, agent_stats, cat_stats) if has_history else _risk_score(full)
        if score is None:
            continue

        full["_risk_score"] = score
        full["_risk_level"] = "alto" if score >= 75 else "medio" if score >= 45 else "baixo"
        full["_history_boost"] = has_history

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


# ── AI analysis ───────────────────────────────────────────────────────────────

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


# ── Handler ───────────────────────────────────────────────────────────────────

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
            has_history = any(t.get("_history_boost") for t in data)
            return make_response(jsonify({
                "data": data,
                "count": len(data),
                "by_risk": counts,
                "history_active": has_history,
            }), 200, CORS)

        if path == "/sync" and method == "POST":
            result = _sync_history()
            return make_response(jsonify(result), 200, CORS)

        if path == "/debug/datastore" and method == "GET":
            app = zcatalyst_sdk.initialize()
            try:
                table = app.datastore().table("AgentStats")
                raw = table.get_paged_rows(max_rows=10)
                return make_response(jsonify(raw if isinstance(raw, dict) else str(raw)), 200, CORS)
            except Exception as e:
                import traceback
                return make_response(jsonify({"error": str(e), "trace": traceback.format_exc()}), 500, CORS)

        if path == "/stats" and method == "GET":
            agent_stats, cat_stats = _get_history_stats()
            return make_response(jsonify({
                "agent_stats": {f"{a}|{c}": r for (a, c), r in agent_stats.items()},
                "cat_stats": cat_stats,
                "cache_active": time.time() < _history_cache["expires"],
            }), 200, CORS)

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
