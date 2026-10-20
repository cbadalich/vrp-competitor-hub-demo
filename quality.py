"""
quality.py: how current and complete each competitor profile is.

For every tracked company this reads files that already exist (the newest
record, its quote check, and the download receipts) and returns plain inputs
plus one status. Nothing is written to records or snapshots.

Inputs, for each profile:
  confirmed_at     when the profile's pages were downloaded and its quotes found
                   (the snapshot the record uses; carried-forward profiles were
                   re-confirmed then, even if first extracted earlier)
  age_days         whole days since confirmed_at
  extracted_at     when the profile was first written
  found / unclear / not_found   field counts (not_found means "not stated on the
                   pages checked", never "the company does not have it")
  pages_read       pages the profile was built from
  failed_or_weak   currently tracked pages that failed, or saved too little text,
                   in the company's latest fetch
  check            the quote check: passed, failed (with the fields), or missing

Status (config/quality.json holds the numbers; the first matching rule wins):
  Needs Review   a field fails the quote check, or there is no quote check
  Stale          pages last confirmed more than stale_after_days ago
  Partial        a core field is not_found, OR 2+ core fields are unclear,
                 OR 2 or fewer pages were read, OR a tracked page failed or was
                 weak in the latest fetch
  Strong         none of the above
Companies with no profile are "Not profiled yet".

page_coverage(company) and field_cause(...) show which configured pages were
attempted, downloaded, failed, thin or used as evidence, and why a field is not
stated: a failed fetch, thin coverage, or not stated on the pages read.

Usage (run from inside the ci-monitor folder):
    python3 quality.py
Prints the table and writes output/profile_quality.csv.
"""

import csv
import json
from datetime import datetime
from pathlib import Path

import fetch
import verify

PROJECT = Path(__file__).parent
CONFIG_FILE = PROJECT / "config" / "quality.json"
OUTPUT_FILE = PROJECT / "output" / "profile_quality.csv"
STATUSES = ("Strong", "Partial", "Needs Review", "Stale", "Not profiled yet")


def settings():
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def receipts(folder):
    return {p.name[: -len(".receipt.json")]: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(folder.glob("*.receipt.json"))} if folder and folder.is_dir() else {}


def confirmed_at(snapshot_dir):
    """The earliest successful download in the snapshot the profile uses."""
    times = [r["fetched_at"] for r in receipts(snapshot_dir).values() if r.get("ok") and r.get("fetched_at")]
    return min(times) if times else None


def latest_fetch_dir(slug):
    folder = fetch.SNAPSHOT_DIR / slug
    runs = sorted(p for p in folder.glob("*") if p.is_dir()) if folder.is_dir() else []
    return runs[-1] if runs else None


def tracked_pages(company):
    return {fetch.page_name(url) for url in (company.get("pages") or company["website"]).split()}


def quote_check(record_path, field_keys):
    """(result, failed fields, how). Live against the saved pages when they are here,
    otherwise the check verify.py stored next to the record (the hosted demo)."""
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if any((PROJECT / record.get("snapshot", "missing")).glob("*.txt")):
        _, results = verify.verify_record(record_path, field_keys)
        how = "checked just now"
    else:
        stored = record_path.with_name(record_path.stem + ".verification.json")
        if not stored.exists():
            return "missing", [], "no saved pages and no stored check"
        data = json.loads(stored.read_text(encoding="utf-8"))
        results, how = data["results"], f"stored check from {data.get('checked_at', 'an earlier run')[:10]}"
    failed = [k for k, r in results.items() if r.get("result") == "unverified"]
    return ("failed" if failed else "passed"), failed, how


def profile_quality(company, now=None):
    """Inputs and status for one company (see the top of this file)."""
    now = now or datetime.now().astimezone()
    rules = settings()
    record_path = verify.newest_record(verify.RECORDS_DIR / company["slug"])
    if record_path is None:
        return {"company": company["name"], "slug": company["slug"], "status": "Not profiled yet",
                "reasons": ["no profile has been extracted yet"]}
    record = json.loads(record_path.read_text(encoding="utf-8"))
    fields = record.get("fields", {})
    snapshot_dir = PROJECT / record.get("snapshot", "missing")

    confirmed = confirmed_at(snapshot_dir)
    age = (now - datetime.fromisoformat(confirmed)).days if confirmed else None
    counts = {s: sum(f.get("status") == s for f in fields.values()) for s in ("found", "unclear", "not_found")}
    tracked = tracked_pages(company)
    latest = latest_fetch_dir(company["slug"])
    failed_or_weak = sorted(name for name, r in receipts(latest).items()
                            if name in tracked and (not r.get("ok") or r.get("warning")))
    check, failed_fields, check_how = quote_check(record_path, list(fields))
    core = rules["core_fields"]
    core_missing = [k for k in core if fields.get(k, {}).get("status", "not_found") == "not_found"]
    core_unclear = [k for k in core if fields.get(k, {}).get("status") == "unclear"]
    pages_read = record.get("pages_read", [])
    distinct = distinct_pages(snapshot_dir, pages_read)
    not_yet_read = sorted(tracked - set(pages_read) - set(failed_or_weak))

    # The rules, in order. The first that applies sets the status.
    reasons = []
    if check != "passed":
        status = "Needs Review"
        reasons.append(f"quote check failed for {', '.join(failed_fields)}" if failed_fields else check_how)
    elif age is None or age > rules["stale_after_days"]:
        status = "Stale"
        reasons.append(f"pages last confirmed {age} days ago (limit {rules['stale_after_days']})"
                       if age is not None else "no successful download date for the profile's pages")
    else:
        if core_missing:
            reasons.append("not stated on the pages checked: " + ", ".join(k.replace("_", " ") for k in core_missing))
        if len(core_unclear) >= rules["partial_if_core_unclear_at_least"]:
            reasons.append("unclear: " + ", ".join(k.replace("_", " ") for k in core_unclear))
        if distinct <= rules["thin_profile_at_most_pages"]:
            same = f" ({len(pages_read)} addresses, redirecting to the same page)" if distinct < len(pages_read) else ""
            reasons.append(f"only {distinct} distinct page{'s' if distinct != 1 else ''} read{same}")
        if len(failed_or_weak) >= rules["partial_if_failed_or_weak_pages_at_least"]:
            reasons.append(f"{len(failed_or_weak)} tracked page{'s' if len(failed_or_weak) != 1 else ''} "
                           f"failed or saved little text in the latest fetch ({', '.join(failed_or_weak)})")
        status = "Partial" if reasons else "Strong"

    notes = [f"{len(not_yet_read)} tracked page(s) not yet in the profile: {', '.join(not_yet_read)}"] if not_yet_read else []
    return {"company": company["name"], "slug": company["slug"], "type": company["type"], "status": status,
            "reasons": reasons, "notes": notes, "confirmed_at": confirmed, "age_days": age,
            "extracted_at": record.get("extracted_at"), **counts, "pages_read": distinct,
            "failed_or_weak": len(failed_or_weak), "failed_or_weak_pages": failed_or_weak,
            "check": check, "check_how": check_how, "record": str(record_path.relative_to(PROJECT))}


# ------------------------------------------------------------ source coverage

def configured_urls(company):
    return (company.get("pages") or company["website"]).split()


def _page_rows(company, folder, used=None):
    """One row per configured page for one snapshot folder, matched by address
    (page file names can carry a host prefix). used maps page name -> fields."""
    by_url = {r.get("requested_url"): (name, r) for name, r in receipts(folder).items()}
    used = used if used is not None else None
    first_final, rows = {}, []
    for url in configured_urls(company):
        name, r = by_url.get(url, (None, None))
        row = {"url": url, "page": name or fetch.page_name(url), "quotes": 0, "fields": [], "note": ""}
        if r is None:
            row["result"] = "Not attempted yet"
        elif not r.get("ok"):
            row["result"], row["note"] = "Failed", fetch.with_meaning(r.get("error"))
        else:
            fields = (used or {}).get(name, {}) if used is not None else {}
            row["quotes"], row["fields"] = sum(fields.values()), sorted(fields)
            final = r.get("final_url") or url
            if final in first_final:
                row["note"] = f"same page as {first_final[final]} (redirect)"
            first_final.setdefault(final, row["page"])
            if r.get("warning"):
                row["result"] = "Thin text"
                row["note"] = (row["note"] + "; " if row["note"] else "") + f"only {r.get('text_characters', 0)} characters of text"
            elif used is None:  # a fetch the profile does not use: no evidence to report
                row["result"] = "Downloaded"
            else:
                row["result"] = "Used in profile" if row["quotes"] else "Downloaded, no quotes used"
        rows.append(row)
    return rows


def page_coverage(company):
    """Which configured pages were attempted, downloaded, failed, thin, and used as
    evidence, for the snapshot behind the profile and (if newer) the latest fetch.
    Read from existing receipts and the record; nothing is stored."""
    record_path = verify.newest_record(verify.RECORDS_DIR / company["slug"])
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path else {}
    snapshot = PROJECT / record["snapshot"] if record else None
    used = {}
    for key, field in record.get("fields", {}).items():
        for e in field.get("evidence", []):
            used.setdefault(e.get("page"), {}).setdefault(key, 0)
            used[e.get("page")][key] += 1
    latest = latest_fetch_dir(company["slug"])
    rows = _page_rows(company, snapshot, used) if snapshot else []
    newer = latest if latest and (snapshot is None or latest.name > snapshot.name) else None
    latest_rows = _page_rows(company, newer, None) if newer else []
    summary = {"configured": len(configured_urls(company)),
               "downloaded": sum(r["result"] in ("Used in profile", "Downloaded, no quotes used", "Thin text") for r in rows),
               "used": sum(r["quotes"] > 0 for r in rows),
               "failed": sum(r["result"] == "Failed" for r in rows),
               "thin": sum(r["result"] == "Thin text" for r in rows)}
    summary["distinct"] = distinct_pages(snapshot, record.get("pages_read", [])) if snapshot else 0
    return {"snapshot": snapshot.name if snapshot else None, "rows": rows,
            "latest": newer.name if newer else None, "latest_rows": latest_rows,
            "pages_read": summary["distinct"], "summary": summary}


def distinct_pages(snapshot_dir, pages):
    """Pages read, counting addresses that redirect to the same page once."""
    rec = receipts(snapshot_dir)
    return len({rec[p].get("final_url") or p for p in pages if p in rec and rec[p].get("ok")} or set(pages))


def field_cause(coverage, status):
    """Why a field is not stated or unclear, from source coverage alone. The first
    rule that applies wins: failed fetch, then thin coverage, then not stated.
    It never says the company lacks the capability."""
    n = coverage["pages_read"]
    lead = ("Not stated on" if status == "not_found" else "Unclear on") + f" the {n} page{'s' if n != 1 else ''} read"
    failed = [r["page"] for r in coverage["rows"] if r["result"] == "Failed"]
    read = {r["url"] for r in coverage["rows"] if r["result"] in ("Used in profile", "Downloaded, no quotes used", "Thin text")}
    failed += [r["page"] for r in coverage["latest_rows"]
               if r["result"] == "Failed" and r["page"] not in failed and r["url"] not in read]
    if failed:
        return (f"{lead}. {len(failed)} configured page{'s' if len(failed) != 1 else ''} could not be read "
                f"({', '.join(failed[:4])}{', ...' if len(failed) > 4 else ''}), so this may be stated there.")
    thin = [r["page"] for r in coverage["rows"] if r["result"] == "Thin text"]
    if n <= settings()["thin_profile_at_most_pages"] or thin:
        why = f"only {n} page{'s' if n != 1 else ''} read" if n <= settings()["thin_profile_at_most_pages"] else \
              f"{len(thin)} page{'s' if len(thin) != 1 else ''} saved too little text ({', '.join(thin)})"
        return f"{lead}; source coverage is thin ({why})."
    return f"{lead}. All configured pages downloaded."


def all_profiles():
    return [profile_quality(c) for c in fetch.load_competitors()]


def main():
    rows = all_profiles()
    print(f"{'Company':<28} {'Status':<17} {'Confirmed':<11} {'Age':>4} {'Fnd':>4} {'Unc':>4} {'NotSt':>6} "
          f"{'Pages':>6} {'Fail/weak':>9}  Quote check")
    for r in rows:
        if r["status"] == "Not profiled yet":
            print(f"{r['company'][:28]:<28} {r['status']:<17}")
            continue
        print(f"{r['company'][:28]:<28} {r['status']:<17} {(r['confirmed_at'] or '')[:10]:<11} {r['age_days']!s:>4} "
              f"{r['found']:>4} {r['unclear']:>4} {r['not_found']:>6} {r['pages_read']:>6} {r['failed_or_weak']:>9}  {r['check']}")
        for reason in r["reasons"]:
            print(f"    - {reason}")
    counts = {s: sum(r["status"] == s for r in rows) for s in STATUSES}
    print("\n" + ", ".join(f"{n} {s}" for s, n in counts.items() if n))
    columns = ["company", "type", "status", "reasons", "confirmed_at", "age_days", "extracted_at", "found", "unclear",
               "not_found", "pages_read", "failed_or_weak", "check", "notes", "record"]
    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({**r, "reasons": "; ".join(r.get("reasons", [])), "notes": "; ".join(r.get("notes", []))})
    print(f"Wrote {OUTPUT_FILE.relative_to(PROJECT)}.")


if __name__ == "__main__":
    main()
