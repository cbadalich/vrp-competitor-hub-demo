"""
candidates.py: shared rules for the discovery track.

A candidate is a company that might belong in config/competitors.csv. Each one
lives in its own folder:
    data/candidates/<slug>/candidate.json            what we know and why
    data/candidates/<slug>/snapshots/<date_time>/    saved copies of its own site

News leads (articles that may mention a candidate) live in data/leads.json.

The rules every discovery script shares are here:
  - a result is matched by website domain AND by name against tracked companies,
    known aliases (config/known_aliases.json), earlier candidates of any status,
    and the owner's seed lists (config/seed_names.json)
  - a new sighting of a known company is added to that company, not saved twice
  - companies already tracked, or on ignored sites, are never added
  - every result a run looks at is logged in data/discovery_runs/ for the quality report
  - a candidate's own site is saved with fetch.py's code and scanned for keywords
  - classification quotes are checked with verify.py's code
"""

import difflib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import fetch
import verify

PROJECT = Path(__file__).parent
DISCOVERY_FILE = PROJECT / "config" / "discovery.json"
CANDIDATES_DIR = PROJECT / "data" / "candidates"
LEADS_FILE = PROJECT / "data" / "leads.json"
RUNS_DIR = PROJECT / "data" / "discovery_runs"
SEEDS_FILE = PROJECT / "config" / "seed_names.json"
ALIASES_FILE = PROJECT / "config" / "known_aliases.json"
TIPS_FILE = PROJECT / "data" / "tip_inbox.json"


def settings():
    return json.loads(DISCOVERY_FILE.read_text(encoding="utf-8"))


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------- identity

# Country endings made of two parts, like example.co.uk. Needed so the main
# domain keeps three parts there (example.co.uk, not co.uk).
TWO_PART_ENDINGS = ("co.uk", "org.uk", "ac.uk", "com.au", "net.au", "org.au", "co.nz",
                    "com.br", "co.jp", "co.kr", "com.sg", "co.za", "co.in", "com.mx")


def domain_of(url):
    """The company's main domain: 'https://shop.OssoVR.com/pricing' becomes 'ossovr.com'.
    Duplicates are judged by this, so a shop or product subdomain is the same company."""
    if "://" not in url:
        url = "https://" + url
    parts = urlparse(url).netloc.lower().split(":")[0].split(".")
    keep = 3 if ".".join(parts[-2:]) in TWO_PART_ENDINGS else 2
    return ".".join(parts[-keep:])


def is_ignored(domain):
    """News sites, social media, directories, universities and government sites are never candidates."""
    s = settings()
    return (any(domain == d or domain.endswith("." + d) for d in s["ignore_domains"])
            or domain.endswith(tuple(s["ignore_suffixes"])))


def tracked():
    """Companies already in config/competitors.csv, as {domain: name}."""
    return {domain_of(c["website"]): c["name"] for c in fetch.load_competitors()}


def load(slug):
    return json.loads((CANDIDATES_DIR / slug / "candidate.json").read_text(encoding="utf-8"))


def save(candidate):
    folder = CANDIDATES_DIR / candidate["slug"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "candidate.json").write_text(json.dumps(candidate, indent=2, ensure_ascii=False), encoding="utf-8")


def all_candidates():
    return [load(p.parent.name) for p in sorted(CANDIDATES_DIR.glob("*/candidate.json"))]


def possible_duplicates(name, domain):
    """Names that look alike but sit on a different website, for example a product sold
    under a parent brand. These are flagged for you to decide, never merged automatically."""
    known = [(n, d) for d, n in tracked().items()] + [(c["name"], c["domain"]) for c in all_candidates()]
    others = {n.lower(): n for n, d in known if d != domain}
    matches = difflib.get_close_matches(name.lower(), list(others), n=3, cutoff=0.75)
    return [others[m] for m in matches]


def new_slug(domain):
    """'ossovr.com' becomes 'ossovr'. Adds -2, -3... if that name is taken."""
    base = re.sub(r"[^a-z0-9]+", "-", domain.rsplit(".", 1)[0]).strip("-")
    taken = {c["slug"] for c in fetch.load_competitors()}
    slug, n = base, 2
    while (CANDIDATES_DIR / slug).exists() or slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


# Where a sighting's company name came from. A name lookup ("<Name> official
# website") must say where the name was seen first (found_in), so a company is
# never counted as discovered when it was only looked up.
ORIGINS = ("topic-search", "news", "award", "seed-list", "lookup", "owner-tip")
NAME_FILLER = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "ab", "gmbh",
               "sa", "plc", "pty", "the", "group", "holdings"}


def name_key(name):
    """'Sim X, Inc.' and 'SimX' both become 'simx'. Used to match names across sources."""
    text = re.sub(r"\(.*?\)", " ", str(name).lower().replace("&", " and "))
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w and w not in NAME_FILLER]
    return "".join(words)


# Words dropped for the looser seed-list match, so 'Pixo VR' matches 'Pixo'.
LOOSE_FILLER = ("vr", "xr", "ar", "mr", "ai", "health", "healthcare", "medical", "sim", "simulation", "simulations",
                "technologies", "technology", "solutions", "systems", "labs", "studios", "studio", "education", "training", "software")


def loose_key(name):
    key = name_key(name)
    changed = True
    while changed:
        changed = False
        for word in LOOSE_FILLER:
            if key.endswith(word) and len(key) - len(word) >= 4:
                key, changed = key[: -len(word)], True
    return key


def known_index():
    """Everything discovery already knows, by website domain and by name.
    Earlier entries win: tracked companies, then known aliases, then candidates
    (any status, including rejected), then the owner's seed lists."""
    domains, names = {}, {}
    def put(hit, domain_list=(), name_list=()):
        for d in domain_list:
            if d:
                domains.setdefault(d, hit)
        for n in name_list:
            if name_key(n):
                names.setdefault(name_key(n), hit)
    competitors = {c["slug"]: c for c in fetch.load_competitors()}
    candidates_by_slug = {c["slug"]: c for c in all_candidates()}
    for c in competitors.values():
        put({"kind": "tracked", "slug": c["slug"], "name": c["name"]}, [domain_of(c["website"])], [c["name"]])
    for a in json.loads(ALIASES_FILE.read_text(encoding="utf-8"))["aliases"] if ALIASES_FILE.exists() else []:
        target = a["belongs_to"]
        if target in competitors:
            hit = {"kind": "tracked", "slug": target, "name": competitors[target]["name"], "via": "known alias: " + a["why"]}
        elif target in candidates_by_slug:
            c = candidates_by_slug[target]
            hit = {"kind": "candidate", "slug": target, "name": c["name"], "status": c["status"], "via": "known alias: " + a["why"]}
        else:
            continue
        put(hit, a.get("domains", []), a.get("names", []))
    for c in candidates_by_slug.values():
        put({"kind": "candidate", "slug": c["slug"], "name": c["name"], "status": c["status"]},
            [c["domain"]], [c["name"], *c.get("aliases", [])])
    seeds = json.loads(SEEDS_FILE.read_text(encoding="utf-8"))["lists"] if SEEDS_FILE.exists() else []
    for lst in seeds:
        for n in lst["names"]:
            put({"kind": "seed-list", "name": n, "list": lst["source"]}, [], [n])
            names.setdefault("~" + loose_key(n), {"kind": "seed-list", "name": n, "list": lst["source"]})
        for w in lst.get("websites", []):
            put({"kind": "seed-list", "name": w, "list": lst["source"]}, [domain_of(w)], [])
    return domains, names


def check_known(name, website):
    """How a search result relates to what we already know. Returns (outcome, hit, how):
      ignored    a news site, directory, university, government site...
      tracked    a tracked company (same website, same name, or a known alias)
      candidate  an earlier candidate of any status (rejected ones stay rejected)
      seed-list  named in the owner's seed lists but never looked at yet
      new        none of the above: new to us
    """
    domain = domain_of(website) if website else ""
    if domain and is_ignored(domain):
        return "ignored", None, domain
    domains, names = known_index()
    if domain and domain in domains:
        return domains[domain]["kind"], domains[domain], "same website"
    if name_key(name) in names:
        return names[name_key(name)]["kind"], names[name_key(name)], "same name"
    if "~" + loose_key(name) in names:
        return "seed-list", names["~" + loose_key(name)], "similar name"
    return "new", None, ""


def add_sighting(name, website, sighting):
    """Records that a company was seen somewhere (a search result, a news item...).

    Returns (outcome, detail, hit). outcome is one of:
      ignored     the website is a news site, directory, university, etc.
      tracked     already tracked (by website, name, or known alias); nothing saved
      seen-again  an earlier candidate (any status); the sighting is added to it
      known-new   a new candidate folder, but the name is in the owner's seed lists
      new         a new candidate folder: new to us
    """
    outcome, hit, how = check_known(name, website)
    if outcome == "ignored":
        return "ignored", how, None
    if outcome == "tracked":
        return "tracked", f"{hit['name']} ({how}{'; ' + hit['via'] if hit.get('via') else ''})", hit

    sighting = {**sighting, "found_at": now()}
    if outcome == "candidate":
        c = load(hit["slug"])
        if how == "same name" and website and domain_of(website) != c["domain"]:
            sighting["website_differs"] = website  # same name on another site: kept for a person to judge
        if not any(s.get("url") == sighting.get("url") and s.get("source") == sighting.get("source")
                   for s in c["sightings"]):
            c["sightings"].append(sighting)
        if name.lower() != c["name"].lower() and name not in c["aliases"]:
            c["aliases"].append(name)
        c["last_seen"] = sighting["found_at"]
        save(c)
        return "seen-again", f"{c['slug']} ({c['status']}, {how})", hit

    domain = domain_of(website)
    host = urlparse(website if "://" in website else "https://" + website).netloc
    candidate = {
        "slug": new_slug(domain),
        "name": name,
        "website": f"https://{host}/",
        "domain": domain,
        "aliases": [],
        "status": "new",
        "first_seen": sighting["found_at"],
        "last_seen": sighting["found_at"],
        "sightings": [sighting],
        "known_from": hit["list"] if outcome == "seed-list" else None,
        "possible_duplicate_of": possible_duplicates(name, domain),
        "snapshot": None,
        "extra_pages": [],
        "signals": {},
        "suggested_pages": [],
        "classification": None,
        "review": None,
    }
    save(candidate)
    return ("known-new" if outcome == "seed-list" else "new"), candidate["slug"], hit


# ------------------------------------------------------------- owner tips

def normalize_website(website):
    """'Example.com/x' becomes 'https://example.com/x'; empty stays empty."""
    website = (website or "").strip()
    if website and "://" not in website:
        website = "https://" + website
    return website


def load_tips():
    return json.loads(TIPS_FILE.read_text(encoding="utf-8")) if TIPS_FILE.exists() else []


def save_tips(tips):
    TIPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TIPS_FILE.write_text(json.dumps(tips, indent=2, ensure_ascii=False), encoding="utf-8")


def tip_check(name, website):
    """The instant 'already known?' check for a tip. Reads files only; downloads nothing.
    Returns {"outcome", "match", "how"}; outcome is new, tracked, candidate, seed-list or ignored."""
    outcome, hit, how = check_known(name or "", normalize_website(website))
    match = None
    if hit:
        match = hit.get("name", "") + (f" ({hit['status']})" if hit.get("status") else "") + \
                (f", {hit['via']}" if hit.get("via") else "") + (f", from {hit['list']}" if hit.get("list") else "")
    elif outcome == "ignored":
        match = f"{how} is a news site, directory, university or similar"
    return {"outcome": outcome, "match": match, "how": how}


def add_tip(name, website, note=""):
    """Saves a lead from the owner to the inbox for the next discovery run. Returns the tip."""
    name, website = (name or "").strip(), normalize_website(website)
    if not name and not website:
        raise ValueError("enter a company name or a website")
    tips = load_tips()
    tip = {"id": f"tip-{now()[:10]}-{len(tips) + 1}", "name": name, "website": website, "note": (note or "").strip(),
           "submitted_at": now(), "check": tip_check(name, website), "status": "waiting"}
    tips.append(tip)
    save_tips(tips)
    return tip


def waiting_tips():
    return [t for t in load_tips() if t["status"] == "waiting"]


def close_tip(tip_id, run, outcome, detail, slug=None):
    """Marks a tip as processed by a discovery run (called by discover.py)."""
    tips = load_tips()
    for tip in tips:
        if tip["id"] == tip_id:
            tip.update(status="processed", processed_at=now(), run=run, outcome=outcome, detail=detail, slug=slug)
            save_tips(tips)
            return True
    return False


# ------------------------------------------------------------- run logs

def log_result(run, entry):
    """Appends one result to data/discovery_runs/<run>.json, so every result a run
    looked at (not only the new ones) can be counted later."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"{run}.json"
    log = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"run": run, "started": now(), "results": [], "leads": []}
    log["results" if "outcome" in entry else "leads"].append({**entry, "at": now()})
    path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")


def load_runs():
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(RUNS_DIR.glob("*.json"))] if RUNS_DIR.is_dir() else []


RELEVANT = ("1st Degree", "2nd Degree", "3rd Degree")
KNOWN_OUTCOMES = ("tracked", "seen-again", "known-new")


def candidate_state(slug):
    """(proposed label, review status, approved label) for one candidate, or Nones."""
    try:
        c = load(slug)
    except FileNotFoundError:
        return None, None, None
    proposed = (c.get("classification") or {}).get("proposed")
    approved = (c.get("review") or {}).get("type") if c["status"] == "approved" else None
    return proposed, c["status"], approved


def mark_repeats(results):
    """A company that came up again later in the SAME run is a repeat, not an earlier
    find: its outcome becomes 'repeat' so it does not count as already known."""
    created, out = set(), []
    for r in results:
        if r["outcome"] in ("new", "known-new") and r.get("slug"):
            created.add(r["slug"])
        elif r["outcome"] == "seen-again" and r.get("slug") in created:
            r = {**r, "outcome": "repeat"}
        elif r["outcome"] == "no-website":
            key = "nw:" + name_key(r["name"])
            if key in created:
                r = {**r, "outcome": "repeat"}
            created.add(key)
        out.append(r)
    return out


def tally(results):
    """Discovery quality numbers for a list of logged results (see DISCOVERY.md, Quality).
    Repeats within a run are counted once (as their first outcome) and left out of the shares."""
    out = {"repeats": sum(r["outcome"] == "repeat" for r in results)}
    results = [r for r in results if r["outcome"] != "repeat"]
    n = len(results)
    out["considered"] = n
    for word in ("ignored", "tracked", "seen-again", "known-new", "new", "no-website"):
        out[word] = sum(r["outcome"] == word for r in results)
    judged = n - out["ignored"]
    out["already_known"] = sum(out[w] for w in KNOWN_OUTCOMES)
    out["already_known_share"] = out["already_known"] / judged if judged else None
    out["ignored_share"] = out["ignored"] / n if n else None
    new = [r for r in results if r["outcome"] == "new"]
    states = [candidate_state(r["slug"]) for r in new if r.get("slug")]
    out["new_proposed_relevant"] = sum(p in RELEVANT for p, _, _ in states)
    out["new_proposed_not_relevant"] = sum(p == "Not Relevant" for p, _, _ in states)
    out["new_needs_review"] = sum(s in ("new", "deferred") for _, s, _ in states)
    out["new_rejected"] = sum(s == "rejected" for _, s, _ in states)
    out["new_relevant_approved"] = sum(a in RELEVANT for _, _, a in states)
    return out


def run_report(log):
    """Overall numbers plus one row per origin and per source/query, for one run log."""
    results = mark_repeats(log.get("results", []))
    by = {}
    for r in results:
        by.setdefault(("origin", r.get("origin", "?")), []).append(r)
        by.setdefault(("query", f"{r.get('source', '')}: {r.get('query', '')}".strip(": ")), []).append(r)
    return {"run": log["run"], "started": log.get("started"), "leads": len(log.get("leads", [])),
            "overall": tally(results),
            "by_origin": {k: tally(v) for (kind, k), v in by.items() if kind == "origin"},
            "by_query": {k: tally(v) for (kind, k), v in by.items() if kind == "query"}}


def baseline_report():
    """The 'before' numbers from candidates saved before run logs existed. Only
    candidates were kept then (not the tracked or ignored results), so this
    counts where each candidate's FIRST sighting came from."""
    rows = []
    for c in all_candidates():
        s = (c.get("sightings") or [{}])[0]
        source, query = s.get("source", ""), s.get("query", "")
        if s.get("origin"):
            origin = s["origin"]
        elif source.startswith(("internal", "earlier research")):
            origin = "seed-list"
        elif re.search(r'^".+" .*official website$', query):
            origin = "lookup"
        else:
            origin = "topic-search" if source.startswith("web search") else "news"
        rows.append({"name": c["name"], "origin": origin, "status": c["status"],
                     "approved": (c.get("review") or {}).get("type") if c["status"] == "approved" else None,
                     "query": query})
    return rows


# -------------------------------------------------------- saved website

def keyword_signals(folder):
    """Which market keywords appear on the saved pages, grouped as in discovery.json.
    A sorting aid for review, not a classification."""
    text = " ".join(p.read_text(encoding="utf-8") for p in folder.glob("*.txt")).lower()
    found = {}
    for group, terms in settings()["keywords"].items():
        hits = [t for t in terms if re.search(r"\b" + re.escape(t.lower()) + r"\b", text)]
        if hits:
            found[group] = hits
    return found


def suggest_pages(folder, website):
    """Links on the saved homepage that look like product or audience pages."""
    home = folder / "home.html"
    if not home.exists():
        return []
    hints = settings()["page_hints"]
    domain = domain_of(website)
    found = []
    for href in re.findall(r'href="([^"#?]+)"', home.read_text(encoding="utf-8")):
        url = urljoin(website, href)
        path = urlparse(url).path.lower()
        if (domain_of(url) == domain and path not in ("", "/")
                and not re.search(r"\.(css|js|png|jpe?g|svg|gif|pdf|xml|json|ico|woff2?)$", path)
                and not re.search(r"blog|news|career|privacy|cookie|terms|login|wp-|tag/|category/", path)
                and any(h in path for h in hints)):
            if "." not in path.rsplit("/", 1)[-1]:  # a folder-style page gets one trailing slash
                url = url.rstrip("/") + "/"
            if url not in found:
                found.append(url)
    return found[:8]


def name_on_site(candidate, folder):
    """Does the company's name (or an alias) appear on its saved homepage? Spaces,
    punctuation and capitals are ignored. None means the homepage was not saved.
    This is how a website found through search proves it belongs to the company."""
    home = folder / "home.txt"
    if not home.exists():
        return None
    squash = lambda text: re.sub(r"[^a-z0-9]", "", text.lower())
    text = squash(home.read_text(encoding="utf-8"))
    return any(squash(n) and squash(n) in text for n in [candidate["name"]] + candidate["aliases"])


def snapshot(slug, extra_pages=()):
    """Saves the candidate's homepage (plus any extra pages) with fetch.py's code,
    then records keyword signals and suggested pages. Returns the receipts."""
    c = load(slug)
    for url in extra_pages:
        if url not in c["extra_pages"]:
            c["extra_pages"].append(url)
    folder = CANDIDATES_DIR / slug / "snapshots" / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    receipts = []
    for i, url in enumerate([c["website"]] + c["extra_pages"]):
        if i:
            time.sleep(fetch.SECONDS_BETWEEN_PAGES)
        receipts.append(fetch.snapshot_page({"name": c["name"]}, url, folder))
    # Keep only extra pages that downloaded, so a dead link is not retried forever.
    ok = {r["requested_url"] for r in receipts if r["ok"]}
    c["extra_pages"] = [u for u in c["extra_pages"] if u in ok]
    c["snapshot"] = str(folder.relative_to(PROJECT))
    c["signals"] = keyword_signals(folder)
    c["name_on_site"] = name_on_site(c, folder)
    c["suggested_pages"] = suggest_pages(folder, c["website"])
    save(c)
    return receipts


# ------------------------------------------------------- classification

def check_classification(c):
    """Checks a candidate's proposed classification. Returns a list of problems (empty = fine)
    and a list of per-quote results from verify.check_quote."""
    cl = c.get("classification")
    if not cl:
        return [], []
    labels = [cat["label"] for cat in settings()["categories"]]
    problems, results = [], []
    if cl.get("proposed") not in labels:
        problems.append(f"proposed '{cl.get('proposed')}' is not one of {labels}")
    if not cl.get("reason"):
        problems.append("no reason given")
    snapshot_dir = PROJECT / (cl.get("snapshot") or "missing")
    if not snapshot_dir.is_dir():
        problems.append(f"snapshot folder missing: {cl.get('snapshot')}")
    evidence = cl.get("evidence") or []
    if cl.get("proposed") not in ("Unclear", None) and not evidence:
        problems.append("a label was proposed but no quotes support it")
    for e in evidence:
        passed, reason, url = verify.check_quote(e, snapshot_dir)
        results.append({"page": e.get("page"), "quote": e.get("quote"), "source_url": url,
                        "passed": passed, "reason": reason})
        if not passed:
            problems.append(f"{e.get('page')}: {reason}")
    return problems, results


# ---------------------------------------------------------------- leads

def load_leads():
    return json.loads(LEADS_FILE.read_text(encoding="utf-8")) if LEADS_FILE.exists() else {}


def save_leads(leads):
    LEADS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEADS_FILE.write_text(json.dumps(leads, indent=2, ensure_ascii=False), encoding="utf-8")
