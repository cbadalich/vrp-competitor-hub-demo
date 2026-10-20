"""
review_candidate.py: record your decision on a discovery candidate.

    python3 review_candidate.py approve SLUG --type "1st Degree" [--page URL --page URL] [--reason "..."]
    python3 review_candidate.py reject  SLUG --reason "..."
    python3 review_candidate.py defer   SLUG [--reason "..."]
    python3 review_candidate.py undo    SLUG
    python3 review_candidate.py batch   DECISIONS.json

A batch file is a list of decisions, each like
    {"slug": "intulect", "decision": "approve", "type": "1st Degree", "pages": [...], "reason": "..."}
and is saved one by one with exactly the same checks as single decisions. The UI's
review table writes such a file to a temporary folder and runs this.

Approve adds ONE new row to the end of config/competitors.csv. Existing rows are
never changed. The one exception is undo: it takes back an approval by removing
the row that approval added (only a row marked "Added from discovery" with the
same slug). Saved pages and records, if any, are kept. The undone decision is
kept in the candidate's review_history. The candidate folder always stays as
the record of why it was added. The UI's decision buttons run this script; it
is the only code that writes review decisions.

Run from inside the ci-monitor folder.
"""

import argparse
import csv
import json
import sys

import candidates
import fetch

DECIDABLE = {"new", "deferred"}


def approve(c, kind, page_list=(), reason=""):
    labels = [label for label in candidates.settings()["tracked_labels"] if label != "Self"]
    if kind not in labels:
        return f"--type must be one of {labels}"
    if c["domain"] in candidates.tracked():
        return f"{c['domain']} is already tracked"
    if c["slug"] in {row["slug"] for row in fetch.load_competitors()}:
        return f"slug '{c['slug']}' is already used in competitors.csv"

    pages = [c["website"]] + [p for p in (page_list or []) if p != c["website"]]
    row = {"slug": c["slug"], "name": c["name"], "website": c["website"], "type": kind,
           "type_note": f"Added from discovery {candidates.now()[:10]}; evidence in data/candidates/{c['slug']}/",
           "pages": " ".join(pages)}

    path = fetch.COMPETITORS_FILE
    needs_newline = not path.read_bytes().endswith(b"\n")
    with open(path, "a", newline="", encoding="utf-8") as f:
        if needs_newline:
            f.write("\n")
        csv.DictWriter(f, fieldnames=list(row), lineterminator="\n").writerow(row)
    c["status"] = "approved"
    c["review"] = {"decision": "approved", "type": kind, "at": candidates.now(),
                   "reason": reason or "", "tracked_slug": c["slug"], "pages": pages}
    return None


def decide(c, decision, kind=None, page_list=(), reason=""):
    """Applies approve / reject / defer to one candidate and saves it.
    Returns None when saved, or a problem message (nothing is changed then)."""
    if c["status"] not in DECIDABLE:
        return f"{c['name']} is already {c['status']}"
    if decision == "approve":
        problem = approve(c, kind, page_list, reason)
        if problem:
            return problem
    elif decision == "reject":
        if not reason:
            return "a reason is required to reject"
        c["status"] = "rejected"
        c["review"] = {"decision": "rejected", "at": candidates.now(), "reason": reason}
    elif decision == "defer":
        c["status"] = "deferred"
        c["review"] = {"decision": "deferred", "at": candidates.now(), "reason": reason}
    else:
        return f"unknown decision '{decision}'"
    candidates.save(c)
    return None


def run_batch(path):
    failures = 0
    for item in json.loads(open(path, encoding="utf-8").read()):
        c = candidates.load(item["slug"])
        problem = decide(c, item["decision"], item.get("type"), item.get("pages") or [], item.get("reason", ""))
        word = {"approve": "APPROVED", "reject": "REJECTED", "defer": "DEFERRED"}.get(item["decision"], "FAIL")
        if problem:
            failures += 1
            print(f"FAIL      {c['name']}: {problem}; nothing changed.", flush=True)
        else:
            detail = f" as {item.get('type')}" if item["decision"] == "approve" else ""
            print(f"{word:<9} {c['name']}{detail}", flush=True)
    print(f"\nDone with {failures} failure(s). Approved companies are fetched with the next refresh.")
    return failures


def undo(c):
    if c["status"] == "approved":
        path = fetch.COMPETITORS_FILE
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames, rows = reader.fieldnames, list(reader)
        keep = [r for r in rows
                if not (r["slug"] == c["slug"] and r["type_note"].startswith("Added from discovery"))]
        if len(keep) != len(rows) - 1:
            return f"expected exactly one discovery row for '{c['slug']}' in competitors.csv, found {len(rows) - len(keep)}"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(keep)
        print(f"Removed {c['name']} from config/competitors.csv.")
    c.setdefault("review_history", []).append({**(c.get("review") or {}), "undone_at": candidates.now()})
    c["status"], c["review"] = "new", None
    print(f"{c['name']} is back in the review queue.")
    return None


def main():
    parser = argparse.ArgumentParser(description="Record a decision on a discovery candidate.")
    parser.add_argument("decision", choices=["approve", "reject", "defer", "undo", "batch"])
    parser.add_argument("slug", help="the candidate's slug, or for batch the decisions file")
    parser.add_argument("--type")
    parser.add_argument("--page", action="append")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()

    if args.decision == "batch":
        sys.exit(1 if run_batch(args.slug) else 0)
    c = candidates.load(args.slug)
    if args.decision == "undo":
        if c["status"] == "new":
            sys.exit(f"FAIL  {c['name']} has no decision to undo; nothing changed.")
        problem = undo(c)
        if problem:
            sys.exit(f"FAIL  {problem}; nothing changed.")
        candidates.save(c)
        return
    problem = decide(c, args.decision, args.type, args.page or [], args.reason)
    if problem:
        sys.exit(f"FAIL  {problem}; nothing changed.")
    if args.decision == "approve":
        print(f"Added {c['name']} to config/competitors.csv as {args.type}.")
        print("Next: fetch it on the Run the check page, then extract it in Claude Code.")
    elif args.decision == "reject":
        print(f"Rejected {c['name']}. It will not come back as a new candidate.")
    else:
        print(f"Deferred {c['name']}.")


if __name__ == "__main__":
    main()
