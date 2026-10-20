"""
insights.py: counts what competitors claim, from the verified quotes already stored.

No web access and no AI. Everything here is deterministic: the same records and
the same config/insights.json always give the same numbers.

How it works:
  1. For each tracked company, take its newest record and the quote check that
     verify.py stored next to it (<record>.verification.json). Only quotes that
     passed the check are used. Summaries written during extraction are NOT
     matched, so every count traces back to the company's own words.
  2. Match each quote against the topic, theme and term patterns in
     config/insights.json.
  3. Count companies (not quotes) per topic, and label each topic with the
     thresholds in the config (common, uncommon, only VRpatients, ...).

A company that does not match a topic has not CLAIMED it on the pages read.
That is not evidence that it lacks the capability.

Usage (run from inside the ci-monitor folder):
    python3 insights.py                      # 1st Degree competitors
    python3 insights.py "2nd Degree" "3rd Degree"
Writes output/insights.csv (one row per topic) and output/insights_evidence.csv
(one row per matching quote, with its source URL).
"""

import csv
import json
import re
import sys
from pathlib import Path

import fetch
import verify

PROJECT = Path(__file__).parent
CONFIG_FILE = PROJECT / "config" / "insights.json"
OUTPUT_DIR = PROJECT / "output"
SELF = "Self"


def settings():
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def _compile(pattern):
    return re.compile(pattern, re.I)


# ------------------------------------------------------------------ The corpus

def company_quotes(company):
    """The newest record's verified quotes for one company, or None if it has no
    record. Returns {"record", "pages_read", "checked", "quotes": [...]} where each
    quote is {"field", "page", "quote", "source_url"}."""
    record_path = verify.newest_record(verify.RECORDS_DIR / company["slug"])
    if record_path is None:
        return None
    record = json.loads(record_path.read_text(encoding="utf-8"))
    stored = record_path.with_name(record_path.stem + ".verification.json")
    if stored.exists():
        results = json.loads(stored.read_text(encoding="utf-8"))["results"]
        checked = True
    else:  # no stored check: run it now if the saved pages are here (read-only)
        _, results = verify.verify_record(record_path, list(record.get("fields", {})))
        checked = any((PROJECT / record.get("snapshot", "missing")).glob("*.txt"))
    quotes = []
    for field, result in results.items():
        for e in result.get("evidence", []):
            if e.get("passed"):
                quotes.append({"field": field, "page": e.get("page", ""), "quote": e["quote"],
                               "source_url": e.get("source_url") or ""})
    return {"record": str(record_path.relative_to(PROJECT)), "pages_read": len(record.get("pages_read", [])),
            "checked": checked, "quotes": quotes}


def corpus(types, leave_out_thin=False):
    """Companies in the chosen degrees, plus VRpatients (type Self).
    Returns a list of dicts: name, slug, type, pages_read, thin, quotes."""
    thin_at = settings()["thresholds"]["thin_profile_at_most_pages"]
    out = []
    for c in fetch.load_competitors():
        if c["type"] not in types and c["type"] != SELF:
            continue
        data = company_quotes(c)
        if data is None:
            continue
        thin = data["pages_read"] <= thin_at
        if thin and leave_out_thin and c["type"] != SELF:
            continue
        out.append({"name": c["name"], "slug": c["slug"], "type": c["type"], "website": c["website"],
                    "thin": thin, **data})
    return out


# ------------------------------------------------------------------ Matching

def _excluded(slug, entry_key, quote, entry, cfg):
    text = quote["quote"]
    if any(_compile(p).search(text) for p in cfg.get("exclude_everywhere", []) + entry.get("exclude", [])):
        return True
    return any(x.get("slug") == slug and x.get("entry") == entry_key and text.startswith(x.get("quote_starts", "\0"))
               for x in cfg.get("manual_exclusions", []))


def entry_matches(entry, companies, fields=None, patterns=None):
    """{company name: [matching quotes]} for one topic, theme or term."""
    cfg = settings()
    compiled = [_compile(p) for p in (patterns or entry["patterns"])]
    hits = {}
    for c in companies:
        for q in c["quotes"]:
            if fields and q["field"] not in fields:
                continue
            if any(p.search(q["quote"]) for p in compiled) and not _excluded(c["slug"], entry["key"], q, entry, cfg):
                hits.setdefault(c["name"], []).append(q)
    return hits


# ------------------------------------------------------------------ Labels

def market_label(count, total):
    t = settings()["thresholds"]
    if count == 0:
        return "None in this set"
    if total and count >= t["common_at_least_share"] * total:
        return "Common"
    if count <= t["uncommon_at_most_companies"]:
        return "Uncommon"
    return "In between"


def vrpatients_label(count, total, vrp_claims):
    market = market_label(count, total)
    if vrp_claims:
        return {"None in this set": "Only VRpatients", "Common": "Common, VRpatients claims it",
                "Uncommon": "Uncommon, VRpatients claims it"}.get(market, "In between, VRpatients claims it")
    return "Competitors claim it, VRpatients does not" if count else "Nobody in this set claims it"


def topic_rows(kind, companies):
    """One row per capability topic ("capabilities") or messaging theme ("messaging")."""
    cfg = settings()
    competitors = [c["name"] for c in companies if c["type"] != SELF]
    fields = None if kind == "capabilities" else cfg["messaging_fields"]
    rows = []
    for entry in cfg[kind]:
        hits = entry_matches(entry, companies, fields=fields)
        named = sorted(n for n in hits if n in competitors)
        vrp = any(c["type"] == SELF and c["name"] in hits for c in companies)
        rows.append({"key": entry["key"], "label": entry["label"], "definition": entry["definition"],
                     "count": len(named), "total": len(competitors), "companies": named,
                     "vrpatients": vrp, "market": market_label(len(named), len(competitors)),
                     "vs_vrpatients": vrpatients_label(len(named), len(competitors), vrp), "hits": hits})
    return sorted(rows, key=lambda r: (-r["count"], r["label"]))


def terminology_rows(companies):
    """{group label: [rows]}, each row one term with the companies that use it."""
    competitors = [c["name"] for c in companies if c["type"] != SELF]
    groups = {}
    for group in settings()["terminology"]:
        rows = []
        for term in group["terms"]:
            entry = {"key": f"{group['key']}:{term['label']}", "patterns": [term["pattern"]]}
            hits = entry_matches(entry, companies)
            named = sorted(n for n in hits if n in competitors)
            rows.append({"label": term["label"], "count": len(named), "total": len(competitors), "companies": named,
                         "vrpatients": any(c["type"] == SELF and c["name"] in hits for c in companies), "hits": hits})
        groups[group["label"]] = sorted(rows, key=lambda r: (-r["count"], r["label"]))
    return groups


def evidence_rows(section, rows, companies):
    """Flat rows for a CSV download: one per matching quote."""
    types = {c["name"]: c["type"] for c in companies}
    return [{"section": section, "entry": r["label"], "company": name, "degree": types.get(name, ""),
             "field": q["field"], "quote": q["quote"], "source_url": q["source_url"]}
            for r in rows for name, quotes in sorted(r["hits"].items()) for q in quotes]


# ------------------------------------------------------------------ Command line

def main():
    types = sys.argv[1:] or ["1st Degree"]
    companies = corpus(types)
    competitors = [c for c in companies if c["type"] != SELF]
    print(f"{len(competitors)} competitors ({', '.join(types)}) plus VRpatients; "
          f"{sum(c['thin'] for c in competitors)} with 2 or fewer pages read.")
    summary, evidence = [], []
    for kind, title in (("capabilities", "Capability"), ("messaging", "Messaging theme")):
        rows = topic_rows(kind, companies)
        print(f"\n{title} rows")
        for r in rows:
            print(f"  {r['count']:>2} of {r['total']}  {'VRp' if r['vrpatients'] else '   '}  {r['label']}  ({r['vs_vrpatients']})")
            summary.append({"section": title, "entry": r["label"], "competitors_claiming": r["count"],
                            "competitors_in_set": r["total"], "market": r["market"],
                            "vrpatients_claims": "yes" if r["vrpatients"] else "no",
                            "vrpatients_vs_market": r["vs_vrpatients"], "companies": "; ".join(r["companies"])})
        evidence += evidence_rows(title, rows, companies)
    for group, rows in terminology_rows(companies).items():
        for r in rows:
            summary.append({"section": f"Terminology: {group}", "entry": r["label"], "competitors_claiming": r["count"],
                            "competitors_in_set": r["total"], "market": "", "vrpatients_claims": "yes" if r["vrpatients"] else "no",
                            "vrpatients_vs_market": "", "companies": "; ".join(r["companies"])})
        evidence += evidence_rows(f"Terminology: {group}", rows, companies)
    OUTPUT_DIR.mkdir(exist_ok=True)
    for name, rows in (("insights.csv", summary), ("insights_evidence.csv", evidence)):
        with open(OUTPUT_DIR / name, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    print(f"\nWrote output/insights.csv ({len(summary)} rows) and output/insights_evidence.csv ({len(evidence)} quotes).")


if __name__ == "__main__":
    main()
