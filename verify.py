"""
verify.py: Step 3 of the pipeline. Check Claude's extraction against the saved pages.

A Claude Code session writes one record per competitor in data/records/<slug>/.
This script does not trust that record. For every field it checks:
  1. the field exists and its status is one of: found, unclear, not_found
  2. a found or unclear field has at least one piece of evidence
  3. each quote really appears, word for word, in the saved page it names
  4. each quote is long enough to mean something (a bare "VR" proves nothing)

It never edits Claude's record. It writes its findings next to it as
<record>.verification.json, adding the real source URL for each quote from the
page's receipt (so Claude cannot put a wrong URL on a claim).

What this script CANNOT check: whether the value is a fair reading of the quote.
That part is your job when you review.

Usage (run from inside the ci-monitor folder):
    python3 verify.py          check the newest record for every competitor
    python3 verify.py ubisim   check the newest record for one competitor
    python3 verify.py data/records/ubisim/2026-09-23_101500.json   check one file
"""

import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).parent
FIELDS_FILE = PROJECT / "config" / "fields.json"
RECORDS_DIR = PROJECT / "data" / "records"

ALLOWED_STATUS = {"found", "unclear", "not_found"}
MIN_QUOTE_CHARACTERS = 15

# Characters that look alike but are different to a computer. Websites use the
# curly versions; people and AI often type the straight ones.
LOOKALIKES = {"‘": "'", "’": "'", "“": '"', "”": '"',
              "–": "-", "—": "-", " ": " ", "™": "", "®": ""}


def normalize(text):
    """Makes spacing and punctuation uniform so a quote matches even across line breaks."""
    for fancy, plain in LOOKALIKES.items():
        text = text.replace(fancy, plain)
    return " ".join(text.split())


def check_quote(evidence, snapshot):
    """Returns (passed, reason, source_url) for one piece of evidence."""
    page = evidence.get("page", "")
    quote = evidence.get("quote", "")
    page_text = snapshot / f"{page}.txt"
    receipt = snapshot / f"{page}.receipt.json"

    if not page_text.exists():
        return False, f"no saved page named '{page}' in this snapshot", None
    url = json.loads(receipt.read_text(encoding="utf-8")).get("final_url") if receipt.exists() else None
    if len(normalize(quote)) < MIN_QUOTE_CHARACTERS:
        return False, f"quote shorter than {MIN_QUOTE_CHARACTERS} characters", url
    if normalize(quote) not in normalize(page_text.read_text(encoding="utf-8")):
        return False, "quote not found in the saved page", url
    return True, "quote found in the saved page", url


def verify_record(record_path, field_keys):
    record = json.loads(record_path.read_text(encoding="utf-8"))
    snapshot = PROJECT / record.get("snapshot", "missing")
    fields = record.get("fields", {})
    results = {}

    if not record.get("snapshot") or not snapshot.is_dir():
        results["_record"] = {"result": "unverified", "problems": [f"snapshot folder missing: {snapshot}"]}

    for key in field_keys:
        field = fields.get(key)
        problems, checked = [], []
        if field is None:
            results[key] = {"result": "unverified", "problems": ["field missing from record"]}
            continue
        status = field.get("status")
        evidence = field.get("evidence") or []
        if status not in ALLOWED_STATUS:
            problems.append(f"status '{status}' is not one of {sorted(ALLOWED_STATUS)}")
        if status in ("found", "unclear") and not evidence:
            problems.append(f"status is '{status}' but there is no evidence")
        for item in evidence:
            passed, reason, url = check_quote(item, snapshot)
            checked.append({"page": item.get("page"), "source_url": url,
                            "quote": item.get("quote"), "passed": passed, "reason": reason})
            if not passed:
                problems.append(f"{item.get('page')}: {reason}")

        if problems:
            result = "unverified"
        elif status == "not_found":
            result = "not_found"
        else:
            result = "verified"
        results[key] = {"status": status, "result": result, "problems": problems, "evidence": checked}

    for extra in sorted(set(fields) - set(field_keys)):
        results[extra] = {"result": "unverified", "problems": ["field is not in config/fields.json"]}
    return record, results


def newest_record(slug_dir):
    records = sorted(p for p in slug_dir.glob("*.json") if not p.name.endswith(".verification.json"))
    return records[-1] if records else None


def main():
    field_keys = [f["key"] for f in json.loads(FIELDS_FILE.read_text(encoding="utf-8"))["extracted_from_page"]]

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg and arg.endswith(".json"):
        targets = [Path(arg)]
    else:
        slug_dirs = [RECORDS_DIR / arg] if arg else sorted(p for p in RECORDS_DIR.glob("*") if p.is_dir())
        targets = [r for r in (newest_record(d) for d in slug_dirs if d.is_dir()) if r]
    if not targets:
        sys.exit("No records found to verify.")

    any_unverified = False
    for path in targets:
        record, results = verify_record(path, field_keys)
        out = path.with_name(path.stem + ".verification.json")
        checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
        out.write_text(json.dumps({"record": str(path.relative_to(PROJECT)) if path.is_relative_to(PROJECT) else str(path),
                                   "checked_at": checked_at, "results": results}, indent=2), encoding="utf-8")
        print(f"\n{record.get('competitor', path.parent.name)}  ({path.name})")
        for key, r in results.items():
            print(f"  {r['result'].upper():<11} {key}")
            for problem in r["problems"]:
                print(f"              - {problem}")
            any_unverified |= r["result"] == "unverified"

    print("\nVERIFIED means every quote was found on the saved page. It does NOT mean the")
    print("value is a fair summary of the quote; check that yourself.")
    sys.exit(1 if any_unverified else 0)


if __name__ == "__main__":
    main()
