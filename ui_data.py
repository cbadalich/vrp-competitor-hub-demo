"""
ui_data.py: read-only helpers that load the project's files for the UI.

Nothing in this file writes, moves or deletes anything. It reads:
  config/competitors.csv and config/fields.json   (who and what we track)
  data/snapshots/<slug>/<date_time>/*.receipt.json (what each fetch did)
  data/records/<slug>/<date_time>.json              (Claude's extractions)
  output/comparison.csv                             (report.py's table)
  config/discovery.json, data/candidates/, data/leads.json  (discovery track)

Quote checks are re-run live with verify.verify_record, which only reads,
so the UI never shows an out-of-date "verified".
"""

import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path

import candidates
import carry_forward
import fetch
import insights
import quality
import verify
from fetch import load_competitors

PROJECT = Path(__file__).parent
SNAPSHOT_DIR = PROJECT / "data" / "snapshots"
RECORDS_DIR = verify.RECORDS_DIR
COMPARISON_FILE = PROJECT / "output" / "comparison.csv"


def saved_pages_present():
    """True when the saved page text is on this computer. It is left out of the
    GitHub repository, so it is missing in the hosted demo."""
    return any(SNAPSHOT_DIR.glob("*/*/*.txt"))


# The hosted demo has no saved page text, so it runs read-only: nothing there can
# fetch, approve or change files. CI_MONITOR_READ_ONLY=1 forces read-only anywhere.
READ_ONLY = os.environ.get("CI_MONITOR_READ_ONLY") == "1" or not saved_pages_present()


def companies():
    return load_competitors()


def field_definitions():
    """The fields Claude extracts, with their definitions, from config/fields.json."""
    return json.loads(verify.FIELDS_FILE.read_text(encoding="utf-8"))["extracted_from_page"]


def field_keys():
    return [f["key"] for f in field_definitions()]


def record_paths(slug):
    """All records for one company, oldest first. Verification files are left out."""
    folder = RECORDS_DIR / slug
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.json") if not p.name.endswith(".verification.json"))


def load_record(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def check_source(path):
    """None when quotes are re-checked live against the saved pages; otherwise the
    date of the stored check that is shown instead (the hosted demo)."""
    path = Path(path)
    snapshot = PROJECT / load_record(path).get("snapshot", "missing")
    if any(snapshot.glob("*.txt")):
        return None
    stored = path.with_name(path.stem + ".verification.json")
    if stored.exists():
        return json.loads(stored.read_text(encoding="utf-8")).get("checked_at", "an earlier run")
    return "never"


def check_record(path):
    """Re-runs verify.py's quote check on one record. Returns {field: result}.
    Where the saved pages are not available, returns the results verify.py stored."""
    path = Path(path)
    source = check_source(path)
    if source is None:
        _, results = verify.verify_record(path, field_keys())
        return results
    stored = path.with_name(path.stem + ".verification.json")
    if source != "never":
        return json.loads(stored.read_text(encoding="utf-8"))["results"]
    return {k: {"result": "unverified", "problems": ["no saved pages and no stored check"], "evidence": []}
            for k in field_keys()}


def data_as_of():
    """The newest extraction date across all records, for the demo banner."""
    dates = [load_record(p).get("extracted_at", "") for c in companies() for p in record_paths(c["slug"])[-1:]]
    return max(dates, default="")


def fetch_summaries(slug):
    """Summarizes every snapshot folder for one company, oldest first:
    when it ran, how many pages downloaded, and what failed or warned."""
    folder = SNAPSHOT_DIR / slug
    runs = sorted(p for p in folder.glob("*") if p.is_dir()) if folder.is_dir() else []
    summaries = []
    for run in runs:
        receipts = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(run.glob("*.receipt.json"))]
        summaries.append({
            "folder": run.name,
            "fetched_at": min((r["fetched_at"] for r in receipts), default=""),
            "pages": len(receipts),
            "ok_pages": sum(1 for r in receipts if r.get("ok")),
            "failures": [f"{r['requested_url']}: {fetch.with_meaning(r.get('error'))}" for r in receipts if not r.get("ok")],
            "warnings": [f"{r['requested_url']}: {r['warning']}" for r in receipts if r.get("warning")],
        })
    return summaries


def latest_fetch(slug):
    """The newest snapshot folder's summary, or None if nothing was ever fetched."""
    summaries = fetch_summaries(slug)
    return summaries[-1] if summaries else None


def explain_first(failures):
    """The plain-English meaning of the first failure in a list of 'url: error. Meaning: ...' lines."""
    for line in failures:
        if ". Meaning: " in line:
            return line.split(". Meaning: ", 1)[1]
    return ""


def company_status(company):
    """Everything the Dashboard needs about one company, plus a plain-English next step."""
    slug = company["slug"]
    records = record_paths(slug)
    summaries = fetch_summaries(slug)
    fetch = summaries[-1] if summaries else None
    # Only a snapshot with at least one downloaded page can be extracted.
    usable = [f["folder"] for f in summaries if f["ok_pages"] > 0]
    status = {"fetch": fetch, "record_path": None, "record_snapshot": None,
              "extracted_at": None, "unverified": [], "needs_extraction": False,
              "pages_changed": []}

    if records:
        record = load_record(records[-1])
        status["record_path"] = records[-1]
        status["record_snapshot"] = Path(record.get("snapshot", "")).name
        status["extracted_at"] = record.get("extracted_at")
        # Set by carry_forward.py when a profile moved to newer pages whose text changed.
        status["pages_changed"] = (record.get("carried_forward") or {}).get("pages_changed", [])
        status["unverified"] = [k for k, r in check_record(records[-1]).items() if r["result"] == "unverified"]

    # Snapshot folders are named by date and time, so a later name means newer pages.
    # With a profile, only a newer fetch where all its pages downloaded counts
    # (carry_forward.py); a partly failed fetch leaves the profile as it is.
    if usable and not records:
        status["needs_extraction"] = True
    elif usable and usable[-1] > (status["record_snapshot"] or ""):
        pages = load_record(records[-1]).get("pages_read", [])
        complete = carry_forward.newest_complete_snapshot(slug, pages)
        status["needs_extraction"] = bool(complete and complete.name > status["record_snapshot"])
        status["pages_failed_newest"] = [] if status["needs_extraction"] else carry_forward.failed_pages(slug, pages)

    if fetch is None:
        status["next_step"] = "Fetch pages"
    elif fetch["ok_pages"] == 0:
        reason = explain_first(fetch["failures"])
        status["next_step"] = "Last fetch got no pages" + (f". {reason}" if reason else ": fix the failures, then fetch again")
    elif not records:
        status["next_step"] = "Extract in Claude Code (no record yet)"
    elif status["needs_extraction"]:
        status["next_step"] = "Extract in Claude Code (newer pages than the record)"
    elif status.get("pages_failed_newest"):
        reason = explain_first(fetch["failures"])
        status["next_step"] = (f"Profile kept: {len(status['pages_failed_newest'])} page(s) failed in the latest fetch"
                               + (f". {reason}" if reason else "; fetch again later"))
    elif status["unverified"]:
        status["next_step"] = "Fix UNVERIFIED fields"
    else:
        status["next_step"] = "Up to date"
    return status


def comparison_table():
    """Reads output/comparison.csv. Returns (rows, built_at, is_stale) or (None, None, False).

    is_stale is True when report.py should be run again: a record was saved after
    the table was built, or the tracked list (competitors.csv) changed since then.
    """
    if not COMPARISON_FILE.exists():
        return None, None, False
    with open(COMPARISON_FILE, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    built = COMPARISON_FILE.stat().st_mtime
    newest_record = max((p.stat().st_mtime for c in companies() for p in record_paths(c["slug"])), default=0)
    tracked_changed = fetch.COMPETITORS_FILE.stat().st_mtime > built
    names_differ = {r["name"] for r in rows} != {c["name"] for c in companies()}
    return rows, datetime.fromtimestamp(built), newest_record > built or tracked_changed or names_differ


# ---------------------------------------------------------------- discovery

def discovery_settings():
    return candidates.settings()


def tracked_labels():
    """The labels a tracked company can have, e.g. Self, 1st Degree, 2nd Degree, 3rd Degree."""
    return candidates.settings()["tracked_labels"]


def candidate_list():
    return candidates.all_candidates()


def candidate_check(candidate):
    """Re-runs the classification check. Returns (problems, per-quote results).
    Where the saved pages are not available, returns the check stored by
    `discover.py check --save`."""
    cl = candidate.get("classification") or {}
    folder = PROJECT / (cl.get("snapshot") or "missing")
    if not cl or any(folder.glob("*.txt")):
        return candidates.check_classification(candidate)
    stored = candidates.CANDIDATES_DIR / candidate["slug"] / "classification_check.json"
    if stored.exists():
        data = json.loads(stored.read_text(encoding="utf-8"))
        return data["problems"], data["results"]
    return ["no saved pages and no stored check"], []


def candidate_fetch_failures(candidate):
    """Pages of the candidate's own site that failed to save, from the receipts."""
    if not candidate.get("snapshot"):
        return ["No pages saved yet."]
    folder = PROJECT / candidate["snapshot"]
    receipts = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.receipt.json"))]
    return [f"{r['requested_url']}: {fetch.with_meaning(r.get('error'))}" for r in receipts if not r.get("ok")]


def unreviewed_leads():
    return [lead for lead in candidates.load_leads().values() if not lead["reviewed"]]


# ----------------------------------------------------------- market overview

def snapshot_day(record):
    """'data/snapshots/simx/2026-10-13_183515' becomes '2026-10-13'."""
    return Path(record.get("snapshot", "")).name[:10]


def pricing_class(field):
    """Published price / Quote on request / Not stated, from a pricing field."""
    value = str(field.get("value") or "")
    if field.get("status") == "not_found" or not value:
        return "Not stated"
    if re.search(r"[$€£]\s?\d|\d[\d,.]*\s?(usd|eur|gbp)\b", value, re.I):
        return "Published price"
    return "Quote on request"


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def whats_new():
    """What the latest refresh changed, compared with each company's previous profile.

    Returns (refresh_day, items, unchanged_names): changes since the last full
    refresh, including any smaller fetches after it. Each item is a dict with
    company, kind ("added" or "changed") and details (a list of short sentences).
    Only reliable signals are used: new companies, platform labels, taglines,
    pricing class, fields appearing or disappearing, and the re-extraction note
    (changes_since_previous). Re-worded summaries are ignored, since two
    extractions of the same page word them differently."""
    newest = {}
    for c in companies():
        records = [load_record(p) for p in record_paths(c["slug"])]
        if records:
            newest[c["name"]] = records
    if not newest:
        return None, [], []
    # The last full refresh is the newest day on which at least half the companies'
    # current profiles were taken; smaller fetches after it (a new page, a retry)
    # are included, so they don't push the refresh's changes off the page.
    counts = {}
    for records in newest.values():
        counts[snapshot_day(records[-1])] = counts.get(snapshot_day(records[-1]), 0) + 1
    full = [day for day, n in counts.items() if n >= len(newest) / 2]
    latest = max(full) if full else max(counts)
    items, unchanged = [], []
    for name, records in newest.items():
        current = records[-1]
        if snapshot_day(current) < latest:
            continue
        earlier = [r for r in records if snapshot_day(r) < latest]
        if not earlier:  # every profile of this company is from the latest refresh
            items.append({"company": name, "kind": "added", "details": ["First profile."]})
            continue
        previous = earlier[-1]
        note = current.get("changes_since_previous", "")
        if current.get("carried_forward") and not note:
            unchanged.append(name)
            continue
        if note:
            if note.lower().startswith("no substantive"):
                unchanged.append(name)
            else:
                items.append({"company": name, "kind": "changed", "details": [note]})
            continue
        details = []
        old, new = previous.get("fields", {}), current.get("fields", {})
        plat_old, plat_new = set(old.get("platforms", {}).get("value") or []), set(new.get("platforms", {}).get("value") or [])
        if plat_new - plat_old:
            details.append("Now lists platforms: " + ", ".join(sorted(plat_new - plat_old)) + ".")
        if plat_old - plat_new:
            details.append("No longer lists platforms: " + ", ".join(sorted(plat_old - plat_new)) + ".")
        if _norm(old.get("tagline", {}).get("value")) != _norm(new.get("tagline", {}).get("value")) \
                and "found" in (old.get("tagline", {}).get("status"), new.get("tagline", {}).get("status")):
            details.append(f"Tagline now: \"{new.get('tagline', {}).get('value') or 'not stated'}\".")
        if pricing_class(old.get("pricing", {})) != pricing_class(new.get("pricing", {})):
            details.append(f"Pricing now: {pricing_class(new.get('pricing', {})).lower()} "
                           f"(was {pricing_class(old.get('pricing', {})).lower()}).")
        for key in new:
            was, now = old.get(key, {}).get("status"), new[key].get("status")
            if was == "not_found" and now == "found":
                details.append(f"{key.replace('_', ' ').capitalize()} is now stated on its pages.")
            elif was == "found" and now == "not_found":
                details.append(f"{key.replace('_', ' ').capitalize()} is no longer stated on its pages.")
        (items.append({"company": name, "kind": "changed", "details": details}) if details else unchanged.append(name))
    return latest, items, unchanged


PLATFORM_LABELS = ["VR", "MR", "AR", "Web", "Desktop", "Mobile", "Physical hardware", "Projection or immersive room"]


def market_snapshot(types):
    """Counts for the chosen degrees, from each company's newest profile.
    Returns (profiles, platform_rows, capability_rows, pricing_rows); each row
    is (label, [company names]). Self (VRpatients) is reported separately."""
    profiles = {}
    for c in companies():
        records = record_paths(c["slug"])
        if records and (c["type"] in types or c["type"] == "Self"):
            profiles[c["name"]] = (c["type"], load_record(records[-1]).get("fields", {}))
    def matches(test):
        return [name for name, (kind, fields) in profiles.items() if test(fields)]
    platform_rows = [(label, matches(lambda f, l=label: f.get("platforms", {}).get("status") in ("found", "unclear")
                                     and l in (f.get("platforms", {}).get("value") or [])))
                     for label in PLATFORM_LABELS]
    # Capability topics come from config/insights.json (the entries marked "overview"),
    # matched against verified quotes, so these counts agree with the Insights page.
    corpus = insights.corpus(types)
    capability_rows = [(r["label"], sorted(r["hits"])) for r in insights.topic_rows("capabilities", corpus)
                       if r["key"] in {e["key"] for e in insights.settings()["capabilities"] if e.get("overview")}]
    pricing_rows = [(label, matches(lambda f, l=label: pricing_class(f.get("pricing", {})) == l))
                    for label in ("Published price", "Quote on request", "Not stated")]
    return profiles, platform_rows, capability_rows, pricing_rows


def discovery_reports():
    """One quality report per discovery run, newest first (see candidates.run_report)."""
    return [candidates.run_report(log) for log in reversed(candidates.load_runs())]


def discovery_baseline():
    """Where the candidates saved before run logs came from (first sighting only)."""
    return candidates.baseline_report()


def profile_qualities():
    """Profile status and its inputs for every tracked company, by slug (see quality.py)."""
    return {q["slug"]: q for q in quality.all_profiles()}
