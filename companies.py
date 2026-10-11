"""
RxBriefing company pages — runs nightly (see .github/workflows/companies.yml).

For every ticker in tickers.csv (except ETFs) it:
  1. Looks up the SEC CIK number (SEC's public ticker list)
  2. Pulls the company's filings from SEC EDGAR, dropping insider forms (3, 4, 5, 144)
  3. Pulls registered clinical trials from ClinicalTrials.gov (the "pipeline")
  4. Pulls approved products from openFDA (Drugs@FDA)
  5. Writes data/companies/<TICKER>.json and site/company/<TICKER>/index.html

All three sources are free and need no API key. SEC asks for a contact
User-Agent; set SEC_USER_AGENT in GitHub secrets, e.g. "RxBriefing you@email.com".

Run it with:  python companies.py            (all companies)
              python companies.py LLY MRNA   (just a few, for testing)
"""

import csv
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
SITE = ROOT / "site"
OUT = ROOT / "data" / "companies"
SITE_URL = os.environ.get("SITE_URL", "https://rxbriefing.com")
UA = os.environ.get("SEC_USER_AGENT", "RxBriefing contact@rxbriefing.com")
HEAD = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}

# Insider / routine forms we do NOT show
EXCLUDE_FORMS = {"3", "4", "5", "3/A", "4/A", "5/A", "144", "144/A"}
FORM_LABELS = {
    "10-K": "Annual report", "10-Q": "Quarterly report", "8-K": "Current report (material event)",
    "S-1": "IPO registration", "S-3": "Shelf registration", "424B5": "Prospectus (offering)",
    "424B4": "Prospectus (IPO)", "DEF 14A": "Proxy statement", "SC 13D": "Activist/large holder (>5%)",
    "SC 13G": "Passive large holder (>5%)", "SC 13G/A": "Large holder update", "20-F": "Annual report (foreign)",
    "6-K": "Report of foreign issuer", "S-8": "Employee stock plan", "10-K/A": "Annual report (amended)",
    "10-Q/A": "Quarterly report (amended)", "8-K/A": "Current report (amended)", "ARS": "Annual report to holders",
}
PHASES = ["Preclinical", "Phase 1", "Phase 2", "Phase 3", "Filed", "Approved"]


def load_tickers():
    with open(ROOT / "tickers.csv", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["sector"] != "etf"]
    return {r["ticker"].strip().upper(): r for r in rows}


def get(url, **kw):
    r = requests.get(url, headers=HEAD, timeout=30, **kw)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- SEC
def cik_map():
    """SEC publishes ticker -> CIK for every listed company."""
    try:
        data = get("https://www.sec.gov/files/company_tickers.json")
        return {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in data.values()}
    except Exception as e:
        print("cik map:", e)
        return {}


def sec_filings(cik, limit=40):
    sub = get(f"https://data.sec.gov/submissions/CIK{cik}.json")
    rec = sub.get("filings", {}).get("recent", {})
    out = []
    for form, date, acc, doc, desc in zip(rec.get("form", []), rec.get("filingDate", []),
                                          rec.get("accessionNumber", []), rec.get("primaryDocument", []),
                                          rec.get("primaryDocDescription", [])):
        if form in EXCLUDE_FORMS:
            continue
        acc_nodash = acc.replace("-", "")
        out.append({
            "form": form, "date": date, "label": FORM_LABELS.get(form, form),
            "title": desc or FORM_LABELS.get(form, form),
            "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{doc}",
            "index": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/",
        })
        if len(out) >= limit:
            break
    return {"name": sub.get("name"), "sic": sub.get("sicDescription"), "state": sub.get("stateOfIncorporation"),
            "fiscal_year_end": sub.get("fiscalYearEnd"), "filings": out}


# ---------------------------------------------------------------- ClinicalTrials.gov
def trials(sponsor_name):
    """Registered interventional trials where the company is the lead sponsor."""
    params = {
        "query.spons": sponsor_name, "pageSize": 100, "format": "json",
        "filter.overallStatus": "RECRUITING,ACTIVE_NOT_RECRUITING,NOT_YET_RECRUITING,ENROLLING_BY_INVITATION,COMPLETED",
        "fields": "NCTId,BriefTitle,Phase,OverallStatus,Condition,InterventionName,PrimaryCompletionDate,LeadSponsorName,StartDate,EnrollmentCount",
    }
    out = []
    try:
        data = get("https://clinicaltrials.gov/api/v2/studies", params=params)
    except Exception as e:
        print("  ct.gov:", e)
        return out
    for st in data.get("studies", []):
        p = st.get("protocolSection", {})
        ident, design, status, cond, arms = (p.get("identificationModule", {}), p.get("designModule", {}),
                                             p.get("statusModule", {}), p.get("conditionsModule", {}),
                                             p.get("armsInterventionsModule", {}))
        lead = p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name", "")
        if sponsor_name.lower().split()[0] not in lead.lower():
            continue  # only trials this company leads
        phases = design.get("phases") or []
        phase = "Phase 3" if any("PHASE3" in x for x in phases) else "Phase 2" if any("PHASE2" in x for x in phases) \
            else "Phase 1" if any("PHASE1" in x for x in phases) else "Early"
        drugs = [i.get("name") for i in arms.get("interventions", []) if i.get("type") in ("DRUG", "BIOLOGICAL", "GENETIC", "DEVICE")]
        out.append({
            "nct": ident.get("nctId"), "title": ident.get("briefTitle"), "phase": phase,
            "status": status.get("overallStatus", "").replace("_", " ").title(),
            "conditions": cond.get("conditions", [])[:3], "drugs": drugs[:3],
            "completion": (status.get("primaryCompletionDateStruct") or {}).get("date"),
            "start": (status.get("startDateStruct") or {}).get("date"),
            "enrollment": (design.get("enrollmentInfo") or {}).get("count"),
        })
    return out


# ---------------------------------------------------------------- openFDA
def approvals(sponsor_name):
    """Approved products listed under this sponsor in Drugs@FDA (openFDA)."""
    words = [w for w in re.split(r"[^a-z0-9]+", sponsor_name.lower()) if w and w not in ("inc", "corp", "co", "the", "plc", "ltd", "ag", "sa", "nv")]
    queries = [f'sponsor_name:"{sponsor_name.upper()}"', f'sponsor_name:"{" ".join(words[:2]).upper()}"', f"sponsor_name:{words[0].upper()}"]
    data = None
    for q in queries:
        try:
            data = get("https://api.fda.gov/drug/drugsfda.json", params={"search": q, "limit": 99})
            if data.get("results"):
                break
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                continue   # no match for this query, try the next
            print("  openfda:", e)
            break
        except Exception as e:
            print("  openfda:", e)
            break
    if not data:
        return []
    seen, out = set(), []
    for app in data.get("results", []):
        for prod in app.get("products", []):
            name = prod.get("brand_name")
            if not name or name in seen or prod.get("marketing_status") not in ("Prescription", "Over-the-counter"):
                continue
            seen.add(name)
            out.append({"brand": name.title(), "generic": (prod.get("active_ingredients") or [{}])[0].get("name", "").title(),
                        "route": prod.get("route", "").title(), "app": app.get("application_number")})
    return sorted(out, key=lambda x: x["brand"])[:60]


# ---------------------------------------------------------------- page
def esc(s):
    return html.escape(str(s or ""))


def render(t, row, info, tr, ap, news):
    by_phase = {p: [] for p in ["Early", "Phase 1", "Phase 2", "Phase 3"]}
    for x in tr:
        by_phase.setdefault(x["phase"], []).append(x)
    counts = [("Preclinical / Early", len(by_phase.get("Early", []))), ("Phase 1", len(by_phase.get("Phase 1", []))),
              ("Phase 2", len(by_phase.get("Phase 2", []))), ("Phase 3", len(by_phase.get("Phase 3", []))),
              ("Approved", len(ap))]
    upcoming = sorted((x for x in tr if x.get("completion") and x["phase"] in ("Phase 2", "Phase 3") and x["status"] != "Completed"),
                      key=lambda x: x["completion"])[:8]

    def trial_rows(lst):
        if not lst:
            return '<p class="mute">No registered trials in this stage.</p>'
        return "".join(
            f'<div class="trial"><a href="https://clinicaltrials.gov/study/{esc(x["nct"])}" target="_blank" rel="noopener">{esc(x["title"])}</a>'
            f'<div class="sub">{esc(", ".join(x["drugs"]) or "—")} · {esc(", ".join(x["conditions"]))} · {esc(x["status"])}'
            f'{" · est. " + esc(x["completion"]) if x.get("completion") else ""}</div></div>' for x in lst[:25])

    stages = "".join(
        f'<button type="button" class="stage" data-stage="{i}"><b>{n}</b><span>{c}</span></button>' for i, (n, c) in enumerate(counts))
    panels = "".join(
        f'<div class="panel" data-stage="{i}" {"" if i == 3 else "hidden"}>{trial_rows(by_phase.get(k, [])) if k else approved_rows(ap)}</div>'
        for i, k in enumerate(["Early", "Phase 1", "Phase 2", "Phase 3", None]))
    cats = "".join(
        f'<li><span class="date">{esc(x["completion"])}</span> <a href="https://clinicaltrials.gov/study/{esc(x["nct"])}" target="_blank" rel="noopener">{esc(x["title"])}</a> <span class="mute">({esc(x["phase"])})</span></li>'
        for x in upcoming) or '<li class="mute">No dated Phase 2/3 completions on record.</li>'
    files = "".join(
        f'<li><span class="form">{esc(f["form"])}</span><span class="date">{esc(f["date"])}</span><a href="{esc(f["url"])}" target="_blank" rel="noopener">{esc(f["label"])}</a></li>'
        for f in info.get("filings", [])[:40]) or '<li class="mute">No filings found.</li>'
    newsrows = "".join(
        f'<li><a href="{esc(n["url"])}" target="_blank" rel="noopener">{esc(n["headline"])}</a> <span class="mute">{esc(n["source"])} · {esc(n["published"][:10])}</span></li>'
        for n in news[:15]) or '<li class="mute">No recent stories.</li>'

    return TEMPLATE.format(
        t=t, name=esc(row["name"]), sector=esc(row["sector"]).title(), legal=esc(info.get("name") or row["name"]),
        sic=esc(info.get("sic") or ""), state=esc(info.get("state") or ""), stages=stages, panels=panels, cats=cats,
        files=files, newsrows=newsrows, ntrials=len(tr), napproved=len(ap),
        updated=datetime.now(timezone.utc).strftime("%b %d, %Y"), site=SITE_URL)


def approved_rows(ap):
    if not ap:
        return '<p class="mute">No approved products listed under this sponsor in Drugs@FDA (partnered or subsidiary products may be listed elsewhere).</p>'
    return '<div class="drugs">' + "".join(
        f'<div class="drug"><b>{esc(a["brand"])}</b><span>{esc(a["generic"])}</span></div>' for a in ap) + "</div>"


TEMPLATE = (ROOT / "company_template.html").read_text() if (ROOT / "company_template.html").exists() else ""


def main():
    tickers = load_tickers()
    only = [a.upper() for a in sys.argv[1:]]
    if only:
        tickers = {k: v for k, v in tickers.items() if k in only}
    OUT.mkdir(parents=True, exist_ok=True)
    ciks = cik_map()
    news_all = {}
    nf = ROOT / "data" / "news.json"
    if nf.exists():
        for s in json.loads(nf.read_text()).get("stories", []):
            for tk in s.get("tickers", []):
                news_all.setdefault(tk, []).append(s)

    index = []
    for t, row in tickers.items():
        print(t, row["name"])
        info = {"filings": []}
        if t in ciks:
            try:
                info = sec_filings(ciks[t])
            except Exception as e:
                print("  sec:", e)
            time.sleep(0.15)  # SEC fair-use: stay under 10 requests/second
        tr = trials(row["name"])
        ap = approvals(row["name"])
        time.sleep(0.3)
        rec = {"ticker": t, "name": row["name"], "sector": row["sector"], "sec": info, "trials": tr,
               "approved": ap, "updated": datetime.now(timezone.utc).isoformat()}
        (OUT / f"{t}.json").write_text(json.dumps(rec))
        d = SITE / "company" / t
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(render(t, row, info, tr, ap, news_all.get(t, [])))
        index.append({"ticker": t, "name": row["name"], "sector": row["sector"], "trials": len(tr), "approved": len(ap)})

    (SITE / "data" / "companies.json").write_text(json.dumps(index))
    print(f"Built {len(index)} company pages")


if __name__ == "__main__":
    main()
