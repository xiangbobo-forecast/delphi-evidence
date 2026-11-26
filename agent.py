"""Delphi Evidence Mining agent (xiangbobo-evidence).

One pass per run:
  markets.json -> pick a few open markets -> search fresh news (Bing News RSS, GDELT)
  -> fetch candidate pages -> Gemini decides if there is ONE new dated, sourced fact
  -> submit it (note + link) -> poll the receipt -> save state + Chinese report.

Rules baked in:
  * Only facts published after the market was created (presumed unpriced otherwise).
  * Max 3 submissions per market (API cap), never both sides of a market.
  * Idempotency-Key = hash(market + url) so a retry cannot double-submit.
  * The cited page must be readable by a plain fetch (the judge has no browser).
  * Stdlib + httpx only. Free Gemini key. No wallet, no trading, no signing.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

API = "https://delphi.fyi/v1"
MARKETS_URL = "https://competition.delphi.fyi/markets.json"
KEY = os.environ.get("DELPHI_API_ACCESS_KEY", "")
GEMINI = os.environ.get("GEMINI_API_KEY", "")
AGENT_ID = "xiangbobo-evidence"
HARNESS = "custom-python"
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "gemini-3.5-flash")
QUERY_MODEL = os.environ.get("QUERY_MODEL", "gemma-4-31b-it")
FALLBACK_MODEL = "gemma-4-31b-it"
MARKETS_PER_RUN = int(os.environ.get("MARKETS_PER_RUN", "4"))
MAX_SUBMITS_PER_RUN = int(os.environ.get("MAX_SUBMITS_PER_RUN", "2"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
# Markets were created around 2026-11-24 09:00 UTC; news before that is presumed priced.
DEFAULT_FRESH_AFTER = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)

ROOT = Path(__file__).parent
STATE = ROOT / "state.json"
LOG = ROOT / "submissions.jsonl"
REPORT = ROOT / "REPORT.md"
UA = "Mozilla/5.0 (compatible; xiangbobo-evidence/1.0; research agent)"

now = datetime.now(timezone.utc)
START = time.time()
TIME_BUDGET = int(os.environ.get("TIME_BUDGET_SEC", str(24 * 60)))


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------------ Delphi API
def dheaders(model: str = JUDGE_MODEL) -> dict:
    return {
        "Authorization": f"Bearer {KEY}",
        "X-Agent-Id": AGENT_ID,
        "X-Agent-Harness": HARNESS,
        "X-Agent-Model": model,
    }


def dget(path: str, **params):
    r = httpx.get(API + path, headers=dheaders(), params=params or None, timeout=30)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:300]}


def submit(market: str, note: str, url: str):
    idem = hashlib.sha256(f"{market}|{url}".encode()).hexdigest()[:64]
    h = dheaders() | {"Idempotency-Key": idem}
    data = {"marketAddress": market, "note": note, "links[]": url}
    r = httpx.post(API + "/submissions", headers=h, data=data, timeout=60)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:300]}


def wait_result(sid: str, max_wait: int = 600) -> dict:
    t0 = time.time()
    res = {}
    while time.time() - t0 < max_wait:
        code, res = dget(f"/submissions/{sid}")
        st = res.get("status")
        if st in ("judged", "rejected", "failed", "unmatched"):
            return res
        time.sleep(30)
    return res


# ------------------------------------------------------------------ search
def _client():
    return httpx.Client(headers={"User-Agent": UA}, timeout=20, follow_redirects=True)


def bing_news(q: str, n: int = 12) -> list[dict]:
    url = ("https://www.bing.com/news/search?format=rss&q=" + urllib.parse.quote_plus(q)
           + '&qft=interval%3d"8"')
    try:
        with _client() as c:
            r = c.get(url)
        root = ET.fromstring(r.content)
    except Exception as e:
        log("bing fail", q, e)
        return []
    out = []
    for it in root.iter("item"):
        link = it.findtext("link") or ""
        m = re.search(r"[?&]url=([^&]+)", link)
        if m:
            link = urllib.parse.unquote(m.group(1))
        dt = None
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate") or "")
        except Exception:
            pass
        out.append({
            "title": html.unescape(it.findtext("title") or ""),
            "desc": html.unescape(re.sub("<[^>]+>", "", it.findtext("description") or ""))[:300],
            "url": link, "date": dt,
        })
    return out[:n]


def gdelt(q: str, n: int = 8) -> list[dict]:
    url = ("https://api.gdeltproject.org/api/v2/doc/doc?mode=artlist&format=json&sort=datedesc"
           f"&maxrecords={n}&timespan=3d&query=" + urllib.parse.quote(q))
    try:
        with _client() as c:
            r = c.get(url)
        arts = r.json().get("articles", [])
    except Exception:
        return []
    out = []
    for a in arts:
        dt = None
        try:
            dt = datetime.strptime(a.get("seendate", ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            pass
        out.append({"title": a.get("title", ""), "desc": "", "url": a.get("url", ""), "date": dt})
    return out


def page_text(url: str, max_chars: int = 4000) -> str:
    try:
        with _client() as c:
            r = c.get(url)
        if r.status_code >= 400:
            return ""
        ct = r.headers.get("content-type", "")
        if "html" not in ct and "text" not in ct:
            return ""
        t = r.text
        t = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>", " ", t)
        t = html.unescape(re.sub(r"<[^>]+>", " ", t))
        t = re.sub(r"\s+", " ", t).strip()
        return t[:max_chars] if len(t) > 400 else ""
    except Exception:
        return ""


# ------------------------------------------------------------------ Gemini
def gemini(prompt: str, model: str, json_out: bool = True) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2}}
    if json_out and not model.startswith("gemma"):
        body["generationConfig"]["responseMimeType"] = "application/json"
    tries = 2 if model.startswith("gemma") else 3
    for attempt in range(tries):
        try:
            r = httpx.post(url, params={"key": GEMINI}, json=body, timeout=75)
            if r.status_code == 429 or r.status_code >= 500:
                log(f"{model} {r.status_code}, retry")
                time.sleep(15 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            log(f"{model} error {str(e)[:150]}")
            time.sleep(10)
    return ""


def ask_json(prompt: str, model: str) -> dict:
    other = "gemini-3.5-flash" if model.startswith("gemma") else FALLBACK_MODEL
    for m in (model, other, "gemini-3.1-flash-lite"):
        txt = gemini(prompt, m)
        mt = re.search(r"\{.*\}", txt or "", re.S)
        if mt:
            try:
                return json.loads(mt.group(0))
            except Exception:
                pass
    return {}


# ------------------------------------------------------------------ logic
def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"markets": {}}


def market_created(m: dict) -> datetime:
    mt = re.search(r"strictly after (\w{3} )?(\d{4}-\d{2}-\d{2}) (\d{1,2}:\d{2} [AP]M) CST", m.get("resolutionCriteria", ""))
    if mt:
        try:
            dt = datetime.strptime(f"{mt.group(2)} {mt.group(3)}", "%Y-%m-%d %I:%M %p")
            return (dt - timedelta(hours=8)).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return DEFAULT_FRESH_AFTER


def parse_time(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        pass
    mt = re.search(r"(\d{4}-\d{2}-\d{2}) (\d{1,2}:\d{2} [AP]M) CST", s)
    if mt:
        dt = datetime.strptime(f"{mt.group(1)} {mt.group(2)}", "%Y-%m-%d %I:%M %p")
        return (dt - timedelta(hours=8)).replace(tzinfo=timezone.utc)
    return None


QUERY_PROMPT = """You help an evidence-mining agent. For this prediction market, write 4 short news search queries
that would surface NEW facts published in the last few days that bear on the outcome. Include at least 2 queries
in the local language of the country involved (e.g. Spanish, German, French, Korean, Japanese, Turkish, Chinese)
when the topic is local. Think about scheduled catalysts, official previews, polls, nowcasts, injuries, statements.

Market: {q}
Resolution: {crit}

Answer JSON only: {{"queries": ["...", "...", "...", "..."]}}"""

JUDGE_PROMPT = """You are the research analyst of an evidence-mining agent for a prediction market.
Your job: decide whether the candidate pages below contain ONE concrete, dated, verifiable fact that is NEW
(published after {fresh} UTC) and would move a careful forecaster's probability on this market.

Market: {q}
Resolution: {crit}
Current market odds YES = {odds:.0%}. Evidence cutoff: {cutoff}.
Facts we already submitted on this market (do not repeat them): {prior}
{side_rule}

Candidate pages (only use facts actually present in the page text):
{pages}

Rules:
- Other agents compete on the same market and a fact someone already submitted earns nothing. So prefer the
  least-covered fact: published in the last 48 hours, from a local-language, official, or niche source.
  A story covered by 3+ outlets in major English media is probably already submitted: skip it unless nothing else.
- Stick exactly to what the page says. No extrapolation, no derived numbers the page does not state
  (the judge marks those "unverifiable").
- Pick at most one fact. It must be stated in the page text, with a date, and be new information
  (a fresh poll, an official preview, a schedule change, a statement, a published data point, etc.).
- Generic background, old facts, opinion pieces without new data, or anything not about this exact market = submit false.
- The note must be factual and neutral in tone: what happened, when, who said/published it, the key numbers,
  plus one short sentence on why it bears on the resolution. Quote the key sentence from the page (translated
  into English if needed, with the original wording in parentheses when short). 600-1200 characters. No hype,
  no instructions to the reader, no predictions of your own.
- "direction" = which outcome the fact makes more likely: "yes" or "no".

Answer JSON only:
{{"submit": true/false, "page_index": <int>, "published": "YYYY-MM-DD", "direction": "yes"|"no",
  "note": "...", "why_new": "..."}}"""


def agent_overlap(a: str, b: str) -> float:
    wa = {w for w in re.findall(r"\w{4,}", a.lower())}
    wb = {w for w in re.findall(r"\w{4,}", b.lower())}
    return len(wa & wb) / max(1, min(len(wa), len(wb)))


STOP = set("will the a an of in on at by for to be is or and per its it with from after before than more less least higher lower report reports reported published release".split())


def heuristic_query(question: str) -> str:
    words = [w for w in re.findall(r"[\w\-\.%≥]+", question) if w.lower() not in STOP]
    return " ".join(words[:8])


def run_market(m: dict, st: dict) -> dict | None:
    addr = m["address"]
    ms = st["markets"].setdefault(addr, {"submitted": [], "direction": None, "checks": 0})
    ms["last_checked"] = now.isoformat()
    ms["checks"] += 1

    code, es = dget(f"/markets/{addr}/entry-state")
    if code != 200 or not es.get("canSubmit"):
        log("skip", addr[:10], es.get("reason") or code)
        if es.get("reason") in ("closed", "cap_reached", "not_open"):
            ms["done"] = es.get("reason")
        return None

    fresh = max(market_created(m), now - timedelta(days=4))
    q = ask_json(QUERY_PROMPT.format(q=m["question"], crit=m.get("resolutionCriteria", "")[:1500]), QUERY_MODEL)
    queries = (q.get("queries") or [])[:4] or [heuristic_query(m["question"])]
    seen, cands = set(), []
    for qq in queries:
        for it in bing_news(qq) + gdelt(qq):
            u = it["url"]
            if not u or u in seen or "news.google" in u:
                continue
            seen.add(u)
            if it["date"] and it["date"] < fresh:
                continue
            if u in [s["url"] for s in ms["submitted"]]:
                continue
            cands.append(it)
    cands.sort(key=lambda x: x["date"] or fresh, reverse=True)
    log(m["question"][:70], "| fresh candidates:", len(cands))
    pages = []
    for it in cands[:10]:
        if len(pages) >= 5:
            break
        txt = page_text(it["url"])
        if txt:
            pages.append((it, txt))
    if not pages:
        return None

    prior = "; ".join(s["summary"][:160] for s in ms["submitted"]) or "none"
    side_rule = (f"We already argued '{ms['direction']}' on this market: only submit a fact pointing '{ms['direction']}'."
                 if ms["direction"] else "")
    def coverage(title):
        return sum(1 for c in cands if agent_overlap(title, c["title"]) >= 0.5)
    ptxt = "\n\n".join(
        f"[{i}] (covered by ~{coverage(it['title'])} outlets) {it['title']} | {it['url']} | listed {it['date'].strftime('%Y-%m-%d %H:%M UTC') if it['date'] else 'date?'}\n{t}"
        for i, (it, t) in enumerate(pages))
    d = ask_json(JUDGE_PROMPT.format(q=m["question"], crit=m.get("resolutionCriteria", "")[:1500],
                                     odds=m.get("odds", {}).get("yes", 0.5), cutoff=m.get("evidenceCutoffAt"),
                                     fresh=fresh.strftime("%Y-%m-%d %H:%M"), prior=prior, side_rule=side_rule,
                                     pages=ptxt), JUDGE_MODEL)
    if not d.get("submit"):
        log("  no new fact")
        return None
    try:
        it, _ = pages[int(d.get("page_index", -1))]
    except Exception:
        return None
    direction = (d.get("direction") or "").lower()
    if ms["direction"] and direction != ms["direction"]:
        log("  opposite side, skip")
        return None
    note = (d.get("note") or "").strip()
    if len(note) < 150:
        return None
    note = note[:3000] + f"\n\nSource: {it['url']} (published {d.get('published') or 'see page'})."
    return {"market": addr, "question": m["question"], "url": it["url"], "note": note,
            "direction": direction, "summary": note[:200]}


def main():
    if not KEY or not GEMINI:
        sys.exit("missing DELPHI_API_ACCESS_KEY or GEMINI_API_KEY")
    st = load_state()
    code, lim = dget("/limits")
    log("limits", code, lim)
    left = int(lim.get("submitsLeftThisHour", 0)) if code == 200 else 0
    markets = httpx.get(MARKETS_URL, timeout=30).json()
    open_m = []
    for m in markets:
        cut = parse_time(m.get("evidenceCutoffAt", ""))
        ms = st["markets"].get(m["address"], {})
        if m.get("status") != "open" or ms.get("done") or len(ms.get("submitted", [])) >= 3:
            continue
        if cut and cut < now + timedelta(minutes=30):
            continue
        open_m.append(m)
    # least recently checked first, then soonest cutoff (catalysts close soon)
    open_m.sort(key=lambda m: (st["markets"].get(m["address"], {}).get("last_checked", ""),
                               m.get("evidenceCutoffAt", "")))
    log(f"{len(open_m)} eligible markets; checking {MARKETS_PER_RUN}")
    done = 0
    for m in open_m[:MARKETS_PER_RUN]:
        if done >= min(MAX_SUBMITS_PER_RUN, left):
            break
        if time.time() - START > TIME_BUDGET:
            log("time budget used, stopping early")
            break
        try:
            sub = run_market(m, st)
        except Exception as e:
            log("market error", e)
            continue
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))
        if not sub:
            continue
        if DRY_RUN:
            log("DRY RUN would submit:", json.dumps(sub, ensure_ascii=False)[:800])
            continue
        code, res = submit(sub["market"], sub["note"], sub["url"])
        log("submit", code, json.dumps(res)[:300])
        sid = res.get("id")
        rec = sub | {"time": now.isoformat(), "http": code, "id": sid, "status": res.get("status"),
                     "code": res.get("code") or (res.get("error") or {}).get("code")}
        if sid:
            done += 1
            ms = st["markets"][sub["market"]]
            ms["submitted"].append({"url": sub["url"], "summary": sub["summary"], "id": sid})
            ms["direction"] = ms["direction"] or sub["direction"]
            STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))
            with LOG.open("a") as f:
                f.write(json.dumps(rec | {"status": "submitted"}, ensure_ascii=False) + "\n")
            r = wait_result(sid, max_wait=240)
            line = (r.get("markets") or [{}])[0]
            rec |= {"status": r.get("status"), "verdict": line.get("verdict"), "reason": line.get("reason"),
                    "bounty": (line.get("bounty") or {}).get("amount"), "settlementUpTo": line.get("settlementUpTo")}
        with LOG.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    refresh_report()


def refresh_report():
    """Re-read every submission's receipt and write a short Chinese report."""
    code, res = dget("/submissions", limit=100)
    items = res.get("items", []) if code == 200 else []
    rows, total, judged, mk = [], 0, 0, set()
    for it in items:
        code2, r = dget(f"/submissions/{it['id']}")
        for line in r.get("markets") or []:
            amt = int((line.get("bounty") or {}).get("amount") or 0)
            total += amt
            if r.get("status") == "judged":
                judged += 1
                mk.add(line.get("marketAddress"))
            rows.append(f"| {r.get('createdAt','')[:16]} | {line.get('marketAddress','')[:10]} | {line.get('verdict')} | {amt/1e6:.2f} | {str(line.get('reason',''))[:80]} |")
    REPORT.write_text(
        "# Delphi 证据挖掘战报\n\n"
        f"更新时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"- 已提交：{len(items)} 条；已评判：{judged} 条，覆盖 {len(mk)} 个市场（门槛：10 条、3 个市场）\n"
        f"- 评判奖励合计：{total/1e6:.2f} USDC.e（24 小时后到账，结算奖励另算）\n\n"
        "| 时间 | 市场 | 结论 | 奖励 USDC | 理由 |\n|---|---|---|---|---|\n" + "\n".join(rows) + "\n")


if __name__ == "__main__":
    main()
