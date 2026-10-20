"""
carry_forward.py: after a fetch, keep profiles whose evidence is still on the pages.

Extraction (a Claude Code session) is the slow part of a refresh. Most weeks most
websites have not changed what they claim, so this script checks each company
whose newest pages are newer than its profile:

    CARRIED  identical   every page's text is the same as before; the profile is
                         carried forward to the new pages unchanged
    CARRIED  changed     some text changed, but every quote in the profile is still
                         on the new pages; carried forward, and the changed pages
                         are listed so a person can look for new claims
    EXTRACT              at least one quote is no longer on the new pages (or a page
                         failed); this company needs a new extraction in Claude Code
    CURRENT              the profile already uses the newest pages
    KEPT                 a page the profile was built from failed to download in the
                         newer fetch (server error, block); the profile is kept as is

A carried-forward profile is written as a NEW record named after the new snapshot
(earlier records are never edited). It keeps the original extraction date and adds
a "carried_forward" note saying where it came from and what changed.

Usage (run from inside the ci-monitor folder, after fetch.py):
    python3 carry_forward.py
    python3 carry_forward.py simx ubisim
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import fetch
import verify

PROJECT = Path(__file__).parent


def newest_usable_snapshot(slug):
    """The newest snapshot folder with at least one page that downloaded."""
    folder = fetch.SNAPSHOT_DIR / slug
    for run in sorted((p for p in folder.glob("*") if p.is_dir()), reverse=True) if folder.is_dir() else []:
        if any(json.loads(r.read_text(encoding="utf-8")).get("ok") for r in run.glob("*.receipt.json")):
            return run
    return None


def ok_pages(folder):
    return {r.name[: -len(".receipt.json")] for r in folder.glob("*.receipt.json")
            if json.loads(r.read_text(encoding="utf-8")).get("ok")}


def newest_complete_snapshot(slug, pages):
    """The newest snapshot in which every page the profile was built from downloaded.
    A fetch where some of those pages failed (a server error, a block) is not a reason
    to replace a profile: comparing against it would look like the claims disappeared."""
    folder = fetch.SNAPSHOT_DIR / slug
    for run in sorted((p for p in folder.glob("*") if p.is_dir()), reverse=True) if folder.is_dir() else []:
        attempted = {r.name[: -len(".receipt.json")] for r in run.glob("*.receipt.json")}
        # Pages the run did not try (removed from tracking) do not block; pages it tried must have downloaded.
        if ok_pages(run) and (set(pages) & attempted) <= ok_pages(run):
            return run
    return None


def failed_pages(slug, pages):
    """Pages of the profile that the newest usable snapshot tried and could not download."""
    newest = newest_usable_snapshot(slug)
    if not newest:
        return []
    attempted = {r.name[: -len(".receipt.json")] for r in newest.glob("*.receipt.json")}
    return sorted((set(pages) & attempted) - ok_pages(newest))


def page_text(folder, page):
    path = folder / f"{page}.txt"
    return path.read_text(encoding="utf-8") if path.exists() else None


def check_company(company):
    """Returns (word, message). Writes a carried-forward record when the evidence holds."""
    slug, name = company["slug"], company["name"]
    record_path = verify.newest_record(verify.RECORDS_DIR / slug)
    newest = newest_usable_snapshot(slug)
    if record_path is None or newest is None:
        return None, None  # nothing to carry: no profile yet, or nothing fetched
    record = json.loads(record_path.read_text(encoding="utf-8"))
    old = PROJECT / record["snapshot"]
    if newest.name <= old.name:
        return "CURRENT", f"{name}: profile already uses the newest pages"

    pages = record.get("pages_read", [])
    complete = newest_complete_snapshot(slug, pages)
    if complete is None or complete.name <= old.name:
        failed = failed_pages(slug, pages)
        return "KEPT", (f"{name}: {len(failed)} page(s) of the profile failed to download in the newest fetch "
                        f"({', '.join(failed)}); profile kept. Fetch again later.")
    newest = complete
    present = [p for p in pages if p in ok_pages(newest)]  # pages no longer tracked drop out
    changed = [p for p in present if page_text(old, p) != page_text(newest, p)]
    missing = []
    for key, field in record.get("fields", {}).items():
        for evidence in field.get("evidence", []):
            passed, reason, _ = verify.check_quote(evidence, newest)
            if not passed:
                missing.append(f"{key} ({evidence.get('page')}: {reason})")
    if missing:
        return "EXTRACT", f"{name}: {len(missing)} quote(s) no longer on the pages, e.g. {missing[0]}"

    carried = dict(record)
    for key in ("changes_since_previous", "revision", "pricing_reextracted_at"):
        carried.pop(key, None)  # these describe the earlier record, not this one
    carried["snapshot"] = str(newest.relative_to(PROJECT))
    carried["pages_read"] = present
    not_read = sorted(ok_pages(newest) - set(present))
    carried["carried_forward"] = {
        "from_record": str(record_path.relative_to(PROJECT)),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "reason": "identical page text" if not changed else "page text changed, every quote still present",
        "pages_changed": changed,
        "pages_dropped": [p for p in pages if p not in present],
        "pages_not_yet_read": not_read,
    }
    out = record_path.parent / f"{newest.name}.json"
    out.write_text(json.dumps(carried, indent=2, ensure_ascii=False), encoding="utf-8")
    if not_read:
        return "CARRIED", (f"{name}: every quote still present; {len(not_read)} newly tracked page(s) not yet read "
                           f"({', '.join(not_read)}): extract again to use them")
    if not changed:
        return "CARRIED", f"{name}: identical pages, profile carried forward"
    return "CARRIED", f"{name}: {len(changed)} page(s) changed ({', '.join(changed)}), every quote still present"


def main():
    companies = fetch.load_competitors()
    if len(sys.argv) > 1:
        companies = [c for c in companies if c["slug"] in sys.argv[1:]]
    tally = {}
    for company in companies:
        word, message = check_company(company)
        if word:
            tally[word] = tally.get(word, 0) + 1
            print(f"{word:<9} {message}", flush=True)
    print("\nDone. " + ", ".join(f"{n} {w.lower()}" for w, n in tally.items())
          + ". Run verify.py next; companies marked EXTRACT need a Claude Code extraction;"
          " KEPT ones only need a later fetch.")


if __name__ == "__main__":
    main()
