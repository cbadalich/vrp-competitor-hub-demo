"""
report.py: Step 4 of the pipeline. Build comparison tables you can open in Excel.

For every company in config/competitors.csv, this script takes the newest record
in data/records/<slug>/ and re-runs the quote check from verify.py (so the report
can never show an out-of-date "verified"). It writes two files:

  output/comparison.csv   one row per company, one column group per field
                          (value, status, source URLs)
  output/evidence.csv     one row per quote: company, field, quote, source URL,
                          and whether the quote was found on the saved page

A company with no record still gets a row that says NO RECORD, so a gap is
visible instead of silently missing.

Usage (run from inside the ci-monitor folder):
    python3 report.py
"""

import csv
import json
import re
from pathlib import Path

import verify
from fetch import load_competitors

PROJECT = Path(__file__).parent
OUTPUT_DIR = PROJECT / "output"


# Where a summary is split into points: after a semicolon, or after a period
# that is followed by a new sentence (a capital letter or a $ amount).
POINT_BREAK = re.compile(r";\s+|(?<=\.)\s+(?=[A-Z$])")


def as_bullets(text):
    """Splits a summary into its separate points, each on its own "- " line."""
    points = [p.strip().rstrip(".;") for p in POINT_BREAK.split(text) if p.strip()]
    return "\n".join(f"- {p[0].upper()}{p[1:]}" for p in points)


def display_value(value, bullets=False):
    """Lists, and summaries marked "display": "bullets" in fields.json, become one
    "- " line per item. Other text is unchanged; missing becomes blank."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(f"- {v}" for v in value)
    if bullets:
        return as_bullets(str(value))
    return str(value)


def display_status(field, result):
    """Combines Claude's status with verify.py's result into one word for the table."""
    if result["result"] == "unverified":
        return "UNVERIFIED"
    return field.get("status", "")


def checked_at(record):
    """The earliest download time among the pages the record was built from."""
    snapshot = PROJECT / record["snapshot"]
    times = []
    for page in record.get("pages_read", []):
        receipt = snapshot / f"{page}.receipt.json"
        if receipt.exists():
            times.append(json.loads(receipt.read_text(encoding="utf-8"))["fetched_at"])
    return min(times) if times else ""


def main():
    fields = json.loads(verify.FIELDS_FILE.read_text(encoding="utf-8"))["extracted_from_page"]
    field_keys = [f["key"] for f in fields]
    bullet_keys = {f["key"] for f in fields if f.get("display") == "bullets"}
    OUTPUT_DIR.mkdir(exist_ok=True)

    header = ["name", "website", "type", "type_note", "checked_at", "record"]
    for key in field_keys:
        header += [key, f"{key}_status", f"{key}_sources"]
    comparison_rows, evidence_rows, problems = [], [], 0

    for company in load_competitors():
        base = {"name": company["name"], "website": company["website"],
                "type": company["type"], "type_note": company["type_note"]}
        record_path = verify.newest_record(verify.RECORDS_DIR / company["slug"])
        if record_path is None:
            comparison_rows.append({**base, "record": "NO RECORD"})
            problems += 1
            continue

        record, results = verify.verify_record(record_path, field_keys)
        row = {**base, "checked_at": checked_at(record),
               "record": str(record_path.relative_to(PROJECT))}
        for key in field_keys:
            field = record.get("fields", {}).get(key, {})
            result = results[key]
            evidence = result.get("evidence", [])
            row[key] = display_value(field.get("value"), bullets=key in bullet_keys)
            row[f"{key}_status"] = display_status(field, result)
            row[f"{key}_sources"] = "\n".join(sorted({e["source_url"] for e in evidence if e["source_url"]}))
            problems += result["result"] == "unverified"
            for e in evidence:
                evidence_rows.append({"name": company["name"], "field": key, "status": field.get("status"),
                                      "quote": e["quote"], "source_url": e["source_url"],
                                      "quote_found": "yes" if e["passed"] else "NO: " + e["reason"],
                                      "note": field.get("note", "")})
        comparison_rows.append(row)

    # utf-8-sig lets Excel on a Mac show symbols like ™ and curly quotes correctly.
    with open(OUTPUT_DIR / "comparison.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        writer.writerows(comparison_rows)
    with open(OUTPUT_DIR / "evidence.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "field", "status", "quote", "source_url", "quote_found", "note"])
        writer.writeheader()
        writer.writerows(evidence_rows)

    print(f"Wrote output/comparison.csv ({len(comparison_rows)} companies) "
          f"and output/evidence.csv ({len(evidence_rows)} quotes).")
    if problems:
        print(f"WARNING: {problems} missing record(s) or UNVERIFIED field(s). Look for NO RECORD or UNVERIFIED in the table.")


if __name__ == "__main__":
    main()
