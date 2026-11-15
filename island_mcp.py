"""
island_mcp.py - a tiny MCP server (stdio, no extra packages) so Claude Desktop chats can read the user's day and log
food / water / weight through the island, without inbox files by hand.

Claude Desktop config (%APPDATA%\\Claude\\claude_desktop_config.json) - the island writes the exact snippet with the
right paths to mcp_snippet.json next to this file:
  {"mcpServers": {"island": {"command": "<python.exe>", "args": ["<island folder>\\island_mcp.py"]}}}

Every write goes through the island's own inbox (same checks, ledger and Undo as always); this script never talks to
QAWIFit or Firebase directly and never reads firebase-key.json.
"""

import datetime as dt
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(HERE, "inbox")
VERSION = "1.0"


def _read(name, default=None):
    try:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def today_info():
    t = _read("today.json", {}) or {}
    out = dict(today=t)
    cards = _read("cards.json", {}) or {}
    d = dt.date.today().isoformat()
    out["review_cards_due"] = sum(1 for c in cards.get("cards", []) if not c.get("done") and c.get("due", d) <= d)
    life = _read("life.json", {}) or {}
    out["deadlines"] = [x for x in life.get("deadlines", []) if not x.get("done") and x.get("due", "") >= d][:5]
    out["todo"] = [x.get("text") for x in life.get("todo", []) if not x.get("done")]
    log = _read("study_log.json", {}) or {}
    out["focus_minutes_today"] = {k: round(v / 60) for k, v in (log.get(d) or {}).items() if k in ("P", "MA", "B", "HI")}
    return out


def search_foods(query, limit=10):
    q = str(query or "").strip().lower()
    if not q:
        return []
    foods = ((_read("qawi_foods.json", {}) or {}).get("foods") or []) + ((_read("qawi_foods_user.json", {}) or {}).get("foods") or [])
    hits = []
    for x in foods:
        en, ar = str(x.get("en", "")), str(x.get("ar", ""))
        if q in en.lower() or q in ar:
            score = (0 if en.lower().startswith(q) else 1, 0 if x.get("src") == "user" else 1, len(en))
            hits.append((score, dict(id=x["id"], en=en, ar=ar, per100g=dict(kcal=x.get("kcal"), protein=x.get("p"),
                                                                            carbs=x.get("c"), fat=x.get("f")),
                                     mine=x.get("src") == "user")))
    hits.sort(key=lambda h: h[0])
    return [h[1] for h in hits[:max(1, min(int(limit or 10), 25))]]


def inbox_request(req, wait=20):
    """Drop a request into the island's inbox and wait for done/ or failed/."""
    os.makedirs(INBOX, exist_ok=True)
    rid = req["id"]
    name = rid + ".json"
    tmp = os.path.join(INBOX, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(req, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(INBOX, name))
    end = time.time() + wait
    while time.time() < end:
        for sub in ("done", "failed"):
            p = os.path.join(INBOX, sub, name)
            if os.path.exists(p):
                j = _read(os.path.join("inbox", sub, name), {}) or {}
                return j.get("result") or dict(ok=False, error=j.get("error", "failed"))
        time.sleep(0.4)
    return dict(ok=False, error="the island didn't answer in time - is it running? (the request is still queued)")


def rid(prefix):
    return f"{prefix}-{int(time.time() * 1000)}"


TOOLS = [
    dict(name="get_today", description="the user's day from his desktop island: plan items (done or not), water, QAWIFit kcal/"
         "protein, review cards due, deadlines, to-do, focus minutes per subject.",
         inputSchema=dict(type="object", properties={})),
    dict(name="search_foods", description="Find QAWIFit food ids (built-in + his own foods) with macros per 100 g. "
         "Use before log_food.", inputSchema=dict(type="object", properties=dict(
             query=dict(type="string", description="English or Arabic name, e.g. 'chicken breast' or 'لبنة'"),
             limit=dict(type="integer", default=10)), required=["query"])),
    dict(name="log_food", description="Log a meal into QAWIFit through the island. ALWAYS show the user the items, grams, "
         "kcal and protein first and wait for his OK. Items: {food: id, grams} or {custom: {en, ar, per100g: {kcal, "
         "protein, carbs, fat, fiber}}, grams, save: true} or {barcode, grams} for a barcode the island already knows. "
         "Never put a barcode in a food name.",
         inputSchema=dict(type="object", properties=dict(
             meal_name=dict(type="string"), tag=dict(type="string", enum=["breakfast", "lunch", "dinner", "snack", "pre", "post"]),
             items=dict(type="array", items=dict(type="object"))), required=["items"])),
    dict(name="add_water", description="Add water to today in QAWIFit (negative ml removes).",
         inputSchema=dict(type="object", properties=dict(ml=dict(type="integer")), required=["ml"])),
    dict(name="log_weight", description="Save today's body weight (kg) to QAWIFit Progress. Ask him first.",
         inputSchema=dict(type="object", properties=dict(kg=dict(type="number")), required=["kg"])),
    dict(name="undo", description="Undo one earlier log_food / log_weight by its request id.",
         inputSchema=dict(type="object", properties=dict(request_id=dict(type="string")), required=["request_id"])),
    dict(name="save_idea", description="Append a line to ideas.md in the island folder.",
         inputSchema=dict(type="object", properties=dict(text=dict(type="string")), required=["text"])),
]


def call(name, a):
    if name == "get_today":
        return today_info()
    if name == "search_foods":
        return search_foods(a.get("query"), a.get("limit", 10))
    if name == "log_food":
        return inbox_request(dict(type="food", id=rid("mcp-food"), **{"from": "claude desktop (mcp)"},
                                  meal=dict(name=a.get("meal_name") or (a.get("tag") or "Meal").capitalize(),
                                            tag=a.get("tag") or "snack"), items=a.get("items") or []))
    if name == "add_water":
        return inbox_request(dict(type="water", id=rid("mcp-water"), ml=int(a.get("ml") or 0), **{"from": "claude desktop (mcp)"}))
    if name == "log_weight":
        return inbox_request(dict(type="weight", id=rid("mcp-weight"), kg=float(a.get("kg") or 0), **{"from": "claude desktop (mcp)"}))
    if name == "undo":
        return inbox_request(dict(type="undo", id=rid("mcp-undo"), target=str(a.get("request_id") or ""), **{"from": "claude desktop (mcp)"}))
    if name == "save_idea":
        txt = str(a.get("text") or "").strip()
        if not txt:
            raise ValueError("empty idea")
        with open(os.path.join(HERE, "ideas.md"), "a", encoding="utf-8") as f:
            now = dt.datetime.now()
            f.write(f"- {now:%Y-%m-%d} {now.strftime('%I:%M %p').lstrip('0')}  {txt}  (from Claude)\n")
        return dict(ok=True)
    raise ValueError(f"unknown tool {name}")


def respond(i, result=None, error=None):
    msg = dict(jsonrpc="2.0", id=i)
    if error is not None:
        msg["error"] = dict(code=-32603, message=str(error))
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        m, i = req.get("method"), req.get("id")
        if i is None:                     # notifications (initialized, cancelled ...): nothing to answer
            continue
        try:
            if m == "initialize":
                pv = (req.get("params") or {}).get("protocolVersion") or "2024-11-05"
                respond(i, dict(protocolVersion=pv, capabilities=dict(tools={}),
                                serverInfo=dict(name="island", version=VERSION)))
            elif m == "tools/list":
                respond(i, dict(tools=TOOLS))
            elif m == "tools/call":
                p = req.get("params") or {}
                try:
                    out = call(p.get("name"), p.get("arguments") or {})
                    err = isinstance(out, dict) and out.get("ok") is False
                    respond(i, dict(content=[dict(type="text", text=json.dumps(out, ensure_ascii=False, indent=1))], isError=err))
                except Exception as e:
                    respond(i, dict(content=[dict(type="text", text=f"error: {e}")], isError=True))
            elif m == "ping":
                respond(i, {})
            else:
                sys.stdout.write(json.dumps(dict(jsonrpc="2.0", id=i, error=dict(code=-32601, message=f"no method {m}"))) + "\n")
                sys.stdout.flush()
        except Exception as e:
            respond(i, error=e)


if __name__ == "__main__":
    main()
