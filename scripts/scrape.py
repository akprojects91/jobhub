#!/usr/bin/env python3
"""Refresh data/live.json for the Bahrain Job Finder (runs in GitHub Actions every 6 hours).
Fetches real Bahrain listings from public sources across common job families
and drops anything older than 30 days."""
import json, re, time, html, os, sys, threading
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urlencode
from concurrent.futures import ThreadPoolExecutor
import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "live.json")
DAYS = 30
FAMILIES = {   # search keywords per job family (general Bahrain listings; the page filters by each visitor's preferences)
    "Banking & finance ops": ["banking operations", "operations officer", "operations analyst", "back office", "payments operations",
                              "settlement", "reconciliation", "credit administration", "credit operations", "trade finance",
                              "treasury operations", "bank"],
    "Compliance, risk & audit": ["KYC", "AML", "client onboarding", "compliance", "financial crime", "internal control", "audit", "risk"],
    "Relationship & client mgmt": ["relationship manager", "corporate banking", "account manager"],
    "Accounting & finance": ["accountant", "finance officer", "financial analyst", "accounts payable"],
    "HR & admin": ["payroll", "HR operations", "HR officer", "recruiter", "government relations", "administration officer",
                   "office manager", "secretary", "receptionist"],
    "IT & software": ["software engineer", "IT support", "network engineer", "data analyst", "cyber security"],
    "Engineering & technical": ["mechanical engineer", "electrical engineer", "civil engineer", "project engineer", "technician"],
    "Sales & business dev": ["sales executive", "business development", "sales manager"],
    "Marketing & media": ["marketing", "digital marketing", "graphic designer", "social media"],
    "Customer service": ["customer service", "call centre"],
    "Healthcare": ["nurse", "pharmacist", "doctor", "medical"],
    "Hospitality & F&B": ["hotel", "restaurant", "chef", "front office"],
    "Retail": ["retail", "store manager", "cashier"],
    "Logistics & supply chain": ["logistics", "procurement", "supply chain", "warehouse", "driver"],
    "Education": ["teacher", "lecturer"],
    "Legal": ["legal", "lawyer"],
    "Project & management": ["project manager", "operations manager"],
}
ROLES = list(dict.fromkeys(r for rs in FAMILIES.values() for r in rs))
ROLE_FAMILY = {r: f for f, rs in FAMILIES.items() for r in rs}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
HDR = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
       "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"}
BH = re.compile(r"bahrain|manama|muharraq|riffa|seef|sanabis|juffair|isa town|hamad town|sitra|budaiya|askar|hidd|amwaj", re.I)

_last, _tl = {}, threading.Lock()
GAP = {"www.linkedin.com": 1.5, "bahrain.tanqeeb.com": 1.0, "jobs.workable.com": 0.5}
def get(url):
    host = urlparse(url).netloc; gap = GAP.get(host, 0.3)
    while True:
        with _tl:
            wait = _last.get(host, 0) + gap - time.time()
            if wait <= 0: _last[host] = time.time(); break
        time.sleep(wait)
    return requests.get(url, headers=HDR, timeout=25)

def words(role):
    return [w for w in re.findall(r"[a-z0-9]+", role.lower()) if w not in {"and", "or", "of", "the", "in", "officer", "specialist", "executive"}]

def src_linkedin(role):
    out = []
    for start in (0, 10, 20, 30):
        q = {"keywords": role, "location": "Bahrain", "start": start, "f_TPR": f"r{DAYS*86400}"}
        r = None
        for attempt in range(3):
            r = get("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + urlencode(q))
            if r.status_code != 429: break
            time.sleep(5 * (attempt + 1))
        if r.status_code == 429: raise RuntimeError("rate limited (429)")
        if r.status_code != 200: raise RuntimeError(f"HTTP {r.status_code}")
        cards = BeautifulSoup(r.text, "html.parser").select("li")
        if not cards: break
        for c in cards:
            t = c.select_one(".base-search-card__title"); a = c.select_one("a.base-card__full-link")
            if not t or not a: continue
            co = c.select_one(".base-search-card__subtitle"); loc = c.select_one(".job-search-card__location"); tm = c.select_one("time")
            out.append(dict(title=t.get_text(strip=True), company=co.get_text(strip=True) if co else "",
                            location=loc.get_text(strip=True) if loc else "Bahrain",
                            posted=(tm.get("datetime") if tm else ""), url=a["href"].split("?")[0]))
        if len(cards) < 10: break
    return out

def src_tanqeeb(role):
    out = []
    for page in (1, 2):
        q = {"keywords": role, "countries[]": "26", "page_no": page, "order_by": "most_recent"}
        r = get("https://bahrain.tanqeeb.com/jobs/search?" + urlencode(q))
        if r.status_code != 200: raise RuntimeError(f"HTTP {r.status_code}")
        arts = BeautifulSoup(r.text, "html.parser").select("article[data-job-id]")
        if not arts: break
        for a in arts:
            if (a.get("data-job-country") or "").strip().lower() != "bahrain": continue
            if a.get("data-job-closed") == "1": continue
            posted = ""
            try: posted = datetime.strptime(a.get("data-job-date", "").strip(), "%d %B %Y").strftime("%Y-%m-%d")
            except Exception: pass
            out.append(dict(title=html.unescape(a.get("data-job-name", "")).strip(),
                            company=html.unescape(a.get("data-job-company", "")).strip() or "Confidential",
                            location=html.unescape(a.get("data-job-location", "Bahrain")).strip(),
                            posted=posted, url=a.get("data-job-share-url") or ("https://bahrain.tanqeeb.com" + a.get("data-job-url", ""))))
        if len(arts) < 20: break
    return out

def src_workable(role):
    out, token = [], None
    for _ in range(3):
        q = {"query": role, "location": "Bahrain"}
        if token: q["pageToken"] = token
        r = get("https://jobs.workable.com/api/v1/jobs?" + urlencode(q))
        if r.status_code != 200: raise RuntimeError(f"HTTP {r.status_code}")
        d = r.json()
        for j in d.get("jobs", []):
            locs = " ".join(j.get("locations") or []) + " " + json.dumps(j.get("location") or {})
            if not BH.search(locs): continue
            text = (j.get("title", "") + " " + (j.get("description") or "")).lower()
            if not any(w in text for w in words(role)): continue
            out.append(dict(title=j.get("title", ""), company=(j.get("company") or {}).get("title", ""),
                            location=(j.get("locations") or ["Bahrain"])[0], posted=(j.get("created") or "")[:10], url=j.get("url")))
        token = d.get("nextPageToken")
        if not token: break
    return out

GREENHOUSE = ["binance", "careem", "tamara", "okx", "bybit", "coinbase", "wise", "gemini", "stripe"]
LEVER = ["crypto", "binance"]
ASHBY = ["rain", "ziina", "airwallex"]
_ats = None
def _ats_all():
    jobs = []
    def gh(s):
        r = get(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs")
        return [dict(title=j["title"], company=s.title(), location=j.get("location", {}).get("name", ""),
                     posted=(j.get("updated_at") or "")[:10], url=j.get("absolute_url")) for j in r.json().get("jobs", [])] if r.ok else []
    def lv(s):
        r = get(f"https://api.lever.co/v0/postings/{s}?mode=json")
        return [dict(title=j["text"], company=s.title(), location=" / ".join([str(j.get("categories", {}).get("location", ""))] + (j.get("categories", {}).get("allLocations") or [])),
                     posted=datetime.fromtimestamp(j.get("createdAt", 0)/1000, timezone.utc).strftime("%Y-%m-%d") if j.get("createdAt") else "",
                     url=j.get("hostedUrl")) for j in r.json()] if r.ok else []
    def ab(s):
        r = get(f"https://api.ashbyhq.com/posting-api/job-board/{s}")
        return [dict(title=j["title"], company=s.title(), location=" / ".join([j.get("location", "")] + [x.get("location", "") for x in j.get("secondaryLocations") or []]),
                     posted=(j.get("publishedAt") or "")[:10], url=j.get("jobUrl")) for j in r.json().get("jobs", [])] if r.ok else []
    with ThreadPoolExecutor(6) as ex:
        futs = [ex.submit(gh, s) for s in GREENHOUSE] + [ex.submit(lv, s) for s in LEVER] + [ex.submit(ab, s) for s in ASHBY]
        for f in futs:
            try: jobs += f.result()
            except Exception: pass
    return [j for j in jobs if BH.search(j["location"] or "")]
_atslock = threading.Lock()
def src_ats(role):
    global _ats
    with _atslock:
        if _ats is None: _ats = _ats_all()
    ws = words(role)
    return [j for j in _ats if ws and sum(w in j["title"].lower() for w in ws) >= max(1, len(ws) // 2)]

SOURCES = {"LinkedIn": src_linkedin, "Tanqeeb": src_tanqeeb, "Workable": src_workable, "Employer careers (ATS)": src_ats}

def norm(s): return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

SNAP = os.path.join(ROOT, "data", "tanqeeb.json")   # Tanqeeb blocks GitHub's servers (403); a box-side copy is merged in

def main():
    global OUT, SOURCES
    if "--only" in sys.argv:   # e.g. --only Tanqeeb --out data/tanqeeb.json (used by the box helper)
        i = sys.argv.index("--only"); SOURCES = {sys.argv[i+1]: SOURCES[sys.argv[i+1]]}
    if "--out" in sys.argv: OUT = os.path.abspath(sys.argv[sys.argv.index("--out") + 1])
    tasks = [(s, r) for r in ROLES for s in SOURCES]
    status = {s: {"count": 0, "errors": []} for s in SOURCES}
    results = {}
    def run(t):
        s, r = t
        try: return s, r, SOURCES[s](r), None
        except Exception as e: return s, r, [], str(e)[:120]
    with ThreadPoolExecutor(6) as ex:
        for s, role, jobs, err in ex.map(run, tasks):
            if err: status[s]["errors"].append(f"{role}: {err}")
            for j in jobs:
                if not j.get("url") or not j.get("title"): continue
                k = norm(j["title"]) + "|" + norm(j["company"])
                if k not in results: results[k] = dict(j, id=k, sources=[], roles=[], links={})
                x = results[k]
                if s not in x["sources"]: x["sources"].append(s); x["links"][s] = j["url"]
                if role not in x["roles"]: x["roles"].append(role)
                if j.get("posted") and (not x.get("posted") or j["posted"] > x["posted"]): x["posted"] = j["posted"]
    if "Tanqeeb" in status and status["Tanqeeb"]["errors"] and not any("Tanqeeb" in x["sources"] for x in results.values()) \
            and OUT != SNAP and os.path.exists(SNAP):
        try:
            snap = json.load(open(SNAP))
            for j in snap.get("jobs", []):
                k = j["id"]
                if k in results:
                    x = results[k]
                    if "Tanqeeb" not in x["sources"]: x["sources"].append("Tanqeeb"); x["links"]["Tanqeeb"] = j["links"].get("Tanqeeb", j["url"])
                    for r in j.get("roles", []):
                        if r not in x["roles"]: x["roles"].append(r)
                else: results[k] = j
            status["Tanqeeb"]["errors"] = [f"blocked from GitHub; using box copy from {snap.get('fetched_at', '?')[:16]} UTC"]
        except Exception as e: print("snapshot merge failed:", e)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DAYS)).strftime("%Y-%m-%d")
    jobs = [j for j in results.values() if not j.get("posted") or j["posted"][:10] >= cutoff]
    for j in jobs:
        j["posted"] = (j.get("posted") or "")[:10]
        j["families"] = list(dict.fromkeys(ROLE_FAMILY.get(r, "Other") for r in j.get("roles", [])))
        for s in j["sources"]: status[s]["count"] += 1
    jobs.sort(key=lambda j: j.get("posted") or "", reverse=True)
    st = [{"source": s, "ok": not (v["errors"] and v["count"] == 0) or (v["count"] > 0), "count": v["count"],
           "note": (v["errors"][0] if v["errors"] and v["errors"][0].startswith("blocked from GitHub") else f"{len(v['errors'])} of {len(ROLES)} searches failed: " + "; ".join(v["errors"][:2]) if v["errors"] else "")} for s, v in status.items()]
    total = len(jobs)
    # Safety: if everything failed, keep the previous file instead of wiping the list
    if total == 0 and os.path.exists(OUT):
        print("No jobs fetched — keeping previous data/live.json"); print(json.dumps(st, indent=1)); return 1
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "roles": ROLES, "families": list(FAMILIES),
                   "status": st, "jobs": jobs}, f, ensure_ascii=False, indent=0)
    print(f"{total} jobs written to {OUT}"); print(json.dumps(st, indent=1))
    return 0

if __name__ == "__main__": sys.exit(main())
