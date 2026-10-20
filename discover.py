"""
discover.py: commands for finding possible new competitors.

    python3 discover.py news
        Reads the news feeds in config/discovery.json and saves new articles as
        leads in data/leads.json. Articles are leads, not candidates: deciding which
        company an article is about takes judgment (a person or a Claude Code session).

    python3 discover.py awards [--run RUN]
        Reads recent small-business research awards (NIH RePORTER, NSF SBIR/STTR) that
        match the market keywords and saves them as leads. Each names the company.

    python3 discover.py add "Company Name" https://company.com --run RUN --origin topic-search \
            --source "web search" --query "the search" --url https://where-it-was-seen --snippet "text as shown"
        Records one result. --origin is where the NAME came from: topic-search, news,
        award, seed-list or lookup. A lookup ("<Name> official website") needs
        --found-in URL: where the name was seen first. Every result is logged in
        data/discovery_runs/RUN.json, whether it turns out new, known or ignored.

    python3 discover.py add-batch RESULTS.json --run RUN
        The same for a list of results (see DISCOVERY.md for the file format).

    python3 discover.py report [RUN] [--baseline]
        Discovery quality numbers for a run (or every run): how many results were
        already known, ignored or new, and what happened to the new ones.

    python3 discover.py snapshot SLUG [--page URL --page URL]
        Saves the candidate's homepage again, plus any extra pages.

    python3 discover.py alias SLUG "Other Name"
        Records another name the company uses (a short name or a rebrand) and
        re-runs the name-on-site check against its saved homepage.

    python3 discover.py rename SLUG "Real Name" --reason "..."
        When a saved website belongs to a different company than the name it was
        found under: renames the candidate and drops the wrong name.

    python3 discover.py check [SLUG] [--save]
        Checks every proposed classification: allowed label, a reason, and quotes that
        really appear on the saved pages. With --save, also stores each result in
        data/candidates/<slug>/classification_check.json, so the UI can show it where
        the saved pages are not available (the hosted demo).

    python3 discover.py leads-reviewed
        Marks every current lead as reviewed (after a person or session went through them).

Run from inside the ci-monitor folder.
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

import candidates
import fetch


def plain_text(html):
    parser = fetch.TextExtractor()
    parser.feed(html or "")
    return " ".join(parser.text().split())


def matched_groups(text):
    """Which keyword groups from discovery.json appear in a piece of text."""
    low = text.lower()
    return {group for group, terms in candidates.settings()["keywords"].items()
            if any(re.search(r"\b" + re.escape(t.lower()) + r"\b", low) for t in terms)}


def cmd_news(args):
    leads = candidates.load_leads()
    new, failures = 0, 0
    for feed in candidates.settings()["news_feeds"]:
        for page in range(1, feed.get("pages", 1) + 1):
            url = feed["url"] + (f"?{feed['page_param']}={page}" if page > 1 else "")
            if page > 1:
                time.sleep(fetch.SECONDS_BETWEEN_PAGES)
            allowed, note = fetch.robots_allows(url)
            if not allowed:
                print(f"FAIL  {feed['name']}: {note}")
                failures += 1
                break
            status, final_url, ctype, raw, error = fetch.download(url)
            if error:
                print(f"FAIL  {feed['name']} page {page}: {fetch.with_meaning(error)}")
                failures += 1
                break
            try:
                items = ET.fromstring(raw).iter("item")
            except ET.ParseError as e:
                print(f"FAIL  {feed['name']} page {page}: feed is not valid RSS ({e})")
                failures += 1
                break
            for item in items:
                link = (item.findtext("link") or "").strip()
                title = plain_text(item.findtext("title"))
                summary = plain_text(item.findtext("description"))[:500]
                groups = matched_groups(f"{title} {summary}")
                # A feed can require keywords: every inner list needs at least one group present.
                if not all(any(g in groups for g in need) for need in feed.get("require", [])):
                    continue
                if not link or link in leads:
                    continue
                if getattr(args, "run", None):
                    candidates.log_result(args.run, {"lead": link, "source": feed["name"], "title": title})
                leads[link] = {"title": title, "link": link, "source": feed["name"],
                               "published": (item.findtext("pubDate") or "").strip(), "summary": summary,
                               "keyword_groups": sorted(groups), "collected_at": candidates.now(),
                               "reviewed": False}
                new += 1
                print(f"NEW   {feed['name']}: {title}")
    candidates.save_leads(leads)
    waiting = sum(1 for lead in leads.values() if not lead["reviewed"])
    print(f"\nDone. {new} new lead(s), {waiting} waiting for review, {failures} failure(s).")
    return 1 if failures else 0


def record_result(item, run):
    """Checks one result against everything known, saves it, logs it. Returns its outcome."""
    origin = item.get("origin")
    if origin not in candidates.ORIGINS:
        print(f"FAIL        {item.get('name')}: origin must be one of {', '.join(candidates.ORIGINS)}")
        return "fail"
    if origin == "lookup" and not item.get("found_in"):
        print(f"FAIL        {item.get('name')}: a lookup needs found_in (where the name was seen first)")
        return "fail"
    if not item.get("website") and candidates.check_known(item["name"], "")[0] == "new":
        # a new name whose own website could not be found: logged, never saved without a site
        candidates.log_result(run, {"name": item["name"], "website": "", "origin": origin, "source": item.get("source", ""),
                                    "query": item.get("query", ""), "url": item.get("url", ""),
                                    "found_in": item.get("found_in", ""), "outcome": "no-website",
                                    "detail": "own website not found", "slug": None})
        print(f"{'NO WEBSITE':<11} {item['name']}: new to us, but its own website was not found; logged only")
        return "no-website"
    sighting = {"source": item.get("source", ""), "query": item.get("query", ""), "url": item.get("url", ""),
                "snippet": item.get("snippet", ""), "website_from": item.get("website_from", "result link"),
                "origin": origin, "found_in": item.get("found_in", ""), "run": run}
    outcome, detail, hit = candidates.add_sighting(item["name"], item.get("website", ""), sighting)
    slug = detail if outcome in ("new", "known-new") else (hit or {}).get("slug")
    candidates.log_result(run, {"name": item["name"], "website": item.get("website", ""), "origin": origin,
                                "source": sighting["source"], "query": sighting["query"], "url": sighting["url"],
                                "found_in": sighting["found_in"], "outcome": outcome, "detail": detail, "slug": slug})
    label = {"known-new": "NEW (KNOWN)"}.get(outcome, outcome.upper())
    print(f"{label:<11} {item['name']} ({item.get('website', '')}) -> {detail}")
    if outcome in ("new", "known-new"):
        for r in candidates.snapshot(detail):
            print(("  OK    " if r["ok"] else "  FAIL  ") + r["requested_url"] + ("" if r["ok"] else f" -> {fetch.with_meaning(r['error'])}"))
        c = candidates.load(detail)
        if c["name_on_site"] is False:
            print(f"  WARNING: '{item['name']}' does not appear on {item.get('website')}; it may be a different company.")
        if c["possible_duplicate_of"]:
            print(f"  WARNING: name looks like {', '.join(c['possible_duplicate_of'])}; check for a duplicate.")
    return outcome


def cmd_add(args):
    outcome = record_result({"name": args.name, "website": args.website, "origin": args.origin, "source": args.source,
                             "query": args.query, "url": args.url, "snippet": args.snippet,
                             "website_from": args.website_from, "found_in": args.found_in}, args.run)
    return 1 if outcome == "fail" else 0


def cmd_add_batch(args):
    items = json.loads(open(args.file, encoding="utf-8").read())
    outcomes = [record_result(item, args.run) for item in items]
    counts = {o: outcomes.count(o) for o in dict.fromkeys(outcomes)}
    print(f"\nDone. {len(items)} result(s): " + ", ".join(f"{n} {o}" for o, n in counts.items())
          + f". Logged in data/discovery_runs/{args.run}.json.")
    return 1 if "fail" in outcomes else 0


AWARD_YEARS = 3  # how far back award records are read


def _post_json(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json",
                                                                  "User-Agent": fetch.USER_AGENT})
    return json.load(urllib.request.urlopen(req, timeout=60))


def award_leads():
    """Small-business research awards that mention the market keywords, as lead dicts.
    NIH RePORTER (public API, no key) and NSF (SBIR/STTR programs only)."""
    leads, year = [], datetime.now().year
    groups = candidates.settings()["keywords"]
    for query in candidates.settings().get("award_queries", []):
        allowed, note = fetch.robots_allows("https://api.reporter.nih.gov/v2/projects/search")
        if not allowed:
            raise RuntimeError(f"NIH RePORTER: {note}")
        data = _post_json("https://api.reporter.nih.gov/v2/projects/search", {
            "criteria": {"advanced_text_search": {"operator": "advanced", "search_field": "projecttitle,abstracttext,terms",
                                                  "search_text": query},
                         "activity_codes": ["R41", "R42", "R43", "R44", "SB1", "U43", "U44"],
                         "fiscal_years": list(range(year - AWARD_YEARS, year + 1))},
            "include_fields": ["ProjectTitle", "Organization", "FiscalYear", "ActivityCode", "ProjectNum", "AbstractText"],
            "limit": 100})
        for r in data.get("results", []):
            leads.append({"link": f"https://reporter.nih.gov/project-details/{r['project_num']}",
                          "title": f"{r['organization']['org_name']}: {r['project_title']}",
                          "source": "NIH RePORTER (SBIR/STTR)", "organization": r["organization"]["org_name"],
                          "published": str(r.get("fiscal_year", "")), "summary": (r.get("abstract_text") or "")[:500]})
        time.sleep(1)  # the API asks for at most one request per second
    for query in candidates.settings().get("award_queries_nsf", []):
        url = ("https://api.nsf.gov/services/v1/awards.json?" + urllib.parse.urlencode(
            {"keyword": query, "dateStart": f"01/01/{year - AWARD_YEARS}",
             "printFields": "id,awardeeName,title,startDate,fundProgramName,abstractText"}))
        allowed, note = fetch.robots_allows(url)
        if not allowed:
            raise RuntimeError(f"NSF awards: {note}")
        status, final_url, ctype, raw, error = fetch.download(url)
        if error:
            raise RuntimeError(f"NSF awards: {error}")
        for a in json.loads(raw).get("response", {}).get("award", []):
            if not re.search(r"SBIR|STTR", a.get("fundProgramName", "")):
                continue
            leads.append({"link": f"https://www.nsf.gov/awardsearch/showAward?AWD_ID={a['id']}",
                          "title": f"{a.get('awardeeName', '')}: {a.get('title', '')}",
                          "source": "NSF awards (SBIR/STTR)", "organization": a.get("awardeeName", ""),
                          "published": a.get("startDate", ""), "summary": (a.get("abstractText") or "")[:500]})
        time.sleep(1)
    # keep awards that mention both a healthcare or public-safety training word and a headset or simulation word
    keep = []
    for lead in leads:
        found = matched_groups(lead["title"] + " " + lead["summary"])
        if found & {"healthcare_training", "public_safety"} and found & {"headset", "simulation"}:
            keep.append({**lead, "keyword_groups": sorted(found)})
    return keep


def cmd_awards(args):
    leads = candidates.load_leads()
    try:
        found = award_leads()
    except Exception as e:  # network or API problem: report it, change nothing
        print(f"FAIL  {e}")
        return 1
    new = 0
    for lead in found:
        if lead["link"] in leads:
            continue
        leads[lead["link"]] = {**lead, "collected_at": candidates.now(), "reviewed": False}
        if args.run:
            candidates.log_result(args.run, {"lead": lead["link"], "source": lead["source"], "title": lead["title"]})
        new += 1
        print(f"NEW   {lead['source']}: {lead['title'][:110]}")
    candidates.save_leads(leads)
    print(f"\nDone. {new} new award lead(s) out of {len(found)} matching.")
    return 0


def pct(x):
    return "n/a" if x is None else f"{x:.0%}"


def print_tally(label, t):
    print(f"  {label[:60]:<60} {t['considered']:>4} {t['ignored']:>4} {t['already_known']:>5} {pct(t['already_known_share']):>6}"
          f" {t['new']:>4} {t['no-website']:>4} {t['new_proposed_relevant']:>4} {t['new_proposed_not_relevant']:>4}"
          f" {t['new_needs_review']:>4} {t['new_relevant_approved']:>4}")


def cmd_report(args):
    if args.baseline:
        rows = candidates.baseline_report()
        print(f"Baseline: {len(rows)} candidates saved before run logs existed (first sighting only).")
        for origin in ("seed-list", "lookup", "topic-search", "news", "award"):
            group = [r for r in rows if r["origin"] == origin]
            if group:
                approved = [r for r in group if r["approved"] in candidates.RELEVANT]
                print(f"  {origin:<13} {len(group):>3} candidates, {len(approved)} approved: "
                      + ", ".join(r["name"] for r in group))
        return 0
    logs = [log for log in candidates.load_runs() if not args.run or log["run"] == args.run]
    if not logs:
        print("No run logs yet. Runs are logged when results are added with --run.")
        return 1
    header = (f"  {'':<60} {'seen':>4} {'ign':>4} {'known':>5} {'known%':>6} {'new':>4} {'noWS':>4} {'rel?':>4} {'notR':>4}"
              f" {'wait':>4} {'NEW+':>4}")
    for log in logs:
        rep = candidates.run_report(log)
        print(f"\nRun {rep['run']} (started {rep['started']}), {rep['leads']} lead(s) collected, "
              f"{rep['overall']['repeats']} repeat(s) within the run left out")
        print(header)
        print_tally("ALL RESULTS", rep["overall"])
        print("  by origin:")
        for k, v in sorted(rep["by_origin"].items()):
            print_tally(k, v)
        print("  by source and query:")
        for k, v in sorted(rep["by_query"].items(), key=lambda kv: -kv[1]["considered"]):
            print_tally(k, v)
    print("\nseen: results looked at. ign: ignored sites. known: already tracked, an earlier candidate, or in the seed"
          "\nlists. new: new to us and saved. noWS: new to us, own website not found. rel?: new ones proposed as 1st/2nd/3rd Degree. notR: proposed Not Relevant."
          "\nwait: waiting for your review. NEW+: new to us AND approved by you (the number that matters).")
    return 0


def cmd_snapshot(args):
    receipts = candidates.snapshot(args.slug, args.page or [])
    failed = [r for r in receipts if not r["ok"]]
    for r in receipts:
        print(("OK    " if r["ok"] else "FAIL  ") + r["requested_url"] + ("" if r["ok"] else f" -> {fetch.with_meaning(r['error'])}"))
    return 1 if failed else 0


def cmd_alias(args):
    c = candidates.load(args.slug)
    if args.name not in c["aliases"]:
        c["aliases"].append(args.name)
    if c.get("snapshot"):
        c["name_on_site"] = candidates.name_on_site(c, candidates.PROJECT / c["snapshot"])
    candidates.save(c)
    print(f"{c['name']}: aliases {c['aliases']}; name on site: {c.get('name_on_site')}")
    return 0 if c.get("name_on_site") is not False else 1


def cmd_rename(args):
    """For a website that turned out to belong to a different company than the name it
    was found under: renames the candidate and drops the wrong name, so a later
    sighting of the real company is not matched to this candidate."""
    c = candidates.load(args.slug)
    old = c["name"]
    c["name"] = args.name
    c["aliases"] = [a for a in c["aliases"] if a.lower() not in (old.lower(), args.name.lower())]
    c.setdefault("renamed", []).append({"from": old, "to": args.name, "at": candidates.now(), "reason": args.reason})
    if c.get("snapshot"):
        c["name_on_site"] = candidates.name_on_site(c, candidates.PROJECT / c["snapshot"])
    candidates.save(c)
    print(f"Renamed {old} -> {args.name} ({args.slug}); name on site: {c.get('name_on_site')}")
    return 0


def cmd_check(args):
    pool = [candidates.load(args.slug)] if args.slug else candidates.all_candidates()
    bad = 0
    for c in pool:
        if not c.get("classification"):
            print(f"NOT YET     {c['name']}")
            continue
        problems, results = candidates.check_classification(c)
        label = c["classification"].get("proposed")
        if args.save:
            (candidates.CANDIDATES_DIR / c["slug"] / "classification_check.json").write_text(
                json.dumps({"checked_at": candidates.now(), "proposed": label, "problems": problems,
                            "results": results}, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"{'UNVERIFIED' if problems else 'VERIFIED':<11} {c['name']}: {label} ({len(results)} quote(s))")
        for p in problems:
            print(f"              - {p}")
        bad += bool(problems)
    return 1 if bad else 0


def cmd_leads_reviewed(args):
    leads = candidates.load_leads()
    count = 0
    for lead in leads.values():
        if not lead["reviewed"]:
            lead["reviewed"], count = True, count + 1
    candidates.save_leads(leads)
    print(f"Marked {count} lead(s) as reviewed.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Discovery commands (see the top of discover.py).")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("news")
    p.add_argument("--run", default=None)
    p.set_defaults(run_cmd=cmd_news)
    p = sub.add_parser("awards")
    p.add_argument("--run", default=None)
    p.set_defaults(run_cmd=cmd_awards)
    p = sub.add_parser("add")
    p.add_argument("name")
    p.add_argument("website")
    p.add_argument("--run", required=True, help="a name for this discovery run, e.g. 2026-10-14-test")
    p.add_argument("--origin", required=True, choices=candidates.ORIGINS)
    p.add_argument("--found-in", default="", help="for a lookup: where the name was seen first")
    p.add_argument("--source", required=True)
    p.add_argument("--query", default="")
    p.add_argument("--url", default="", help="where the company was seen")
    p.add_argument("--website-from", default="result link",
                   help="how the website was found: 'result link' or 'search summary'")
    p.add_argument("--snippet", default="", help="the text shown there, copied as written")
    p.set_defaults(run_cmd=cmd_add)
    p = sub.add_parser("add-batch")
    p.add_argument("file")
    p.add_argument("--run", required=True)
    p.set_defaults(run_cmd=cmd_add_batch)
    p = sub.add_parser("report")
    p.add_argument("run", nargs="?")
    p.add_argument("--baseline", action="store_true")
    p.set_defaults(run_cmd=cmd_report)
    p = sub.add_parser("snapshot")
    p.add_argument("slug")
    p.add_argument("--page", action="append")
    p.set_defaults(run_cmd=cmd_snapshot)
    p = sub.add_parser("alias")
    p.add_argument("slug")
    p.add_argument("name")
    p.set_defaults(run_cmd=cmd_alias)
    p = sub.add_parser("rename")
    p.add_argument("slug")
    p.add_argument("name")
    p.add_argument("--reason", required=True)
    p.set_defaults(run_cmd=cmd_rename)
    p = sub.add_parser("check")
    p.add_argument("slug", nargs="?")
    p.add_argument("--save", action="store_true")
    p.set_defaults(run_cmd=cmd_check)
    sub.add_parser("leads-reviewed").set_defaults(run_cmd=cmd_leads_reviewed)
    args = parser.parse_args()
    sys.exit(args.run_cmd(args))


if __name__ == "__main__":
    main()
