"""
fetch.py: Step 1 of the pipeline. Download public web pages and save them.

For each competitor in config/competitors.csv, this script:
  1. checks the site's robots.txt to confirm automated visits are allowed,
  2. downloads each page listed in the "pages" column,
  3. saves three files per page in data/snapshots/<slug>/<date_time>/:
       <page>.html          the raw page exactly as the website sent it
       <page>.txt           the readable text (roughly what a person sees)
       <page>.receipt.json  where it came from, when, and whether it worked

It never overwrites an earlier snapshot. A failure still produces a receipt,
so a missing page is recorded instead of silently skipped.

Speed without being less polite: several websites are fetched at the same time
(PARALLEL_SITES), but pages on any one website are still fetched one at a time
with a pause between them (the site's own robots.txt Crawl-delay if it sets
one, otherwise SECONDS_BETWEEN_PAGES). Each site's robots.txt is read once per run.

Usage (run from inside the ci-monitor folder):
    python3 fetch.py          fetch every competitor
    python3 fetch.py simx     fetch one competitor, by its slug
    python3 fetch.py simx ubisim oms   fetch several competitors
"""

import csv
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

PROJECT = Path(__file__).parent
COMPETITORS_FILE = PROJECT / "config" / "competitors.csv"
SNAPSHOT_DIR = PROJECT / "data" / "snapshots"

# How this tool identifies itself to websites. Some sites (SimX, via Cloudflare)
# refuse requests that don't look like a browser, so this starts like a browser
# and ends by naming the tool.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 ci-monitor/0.1 (course project)"
)
TIMEOUT_SECONDS = 20
SECONDS_BETWEEN_PAGES = 10  # pause between pages on the same site, unless its robots.txt sets a Crawl-delay
PARALLEL_SITES = 8          # how many different websites are fetched at the same time
SHORT_TEXT_WARNING = 500    # less text than this usually means the page is built by JavaScript


class TextExtractor(HTMLParser):
    """Reads HTML and keeps only the visible words, plus the title and meta description."""

    SKIP = {"script", "style", "noscript", "svg", "template"}  # never visible text
    BLOCK = {"p", "div", "section", "article", "header", "footer", "nav", "main",
             "aside", "li", "ul", "ol", "br", "tr", "td", "th", "form",
             "h1", "h2", "h3", "h4", "h5", "h6"}  # start a new line

    # Animated number counters ("60%") keep the real number in one of these settings
    # and show a placeholder such as "0%" until JavaScript animates it.
    COUNTER_ATTRS = ("data-end", "data-target", "data-count", "data-to")
    VOID = {"br", "img", "hr", "input", "meta", "link", "source", "wbr", "area", "col", "embed"}

    def __init__(self):
        super().__init__()
        self.skip_depth = 0
        self.counter_depth = 0  # >0 while inside a counter element
        self.in_title = False
        self.title = ""
        self.meta_description = ""
        self.pieces = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag == "title" and self.skip_depth == 0:  # ignore <title> labels inside icons
            self.in_title = True
        elif tag == "meta":
            label = (a.get("name") or a.get("property") or "").lower()
            if label in ("description", "og:description") and not self.meta_description:
                self.meta_description = (a.get("content") or "").strip()
        if tag in self.BLOCK:
            self.pieces.append("\n")
        # Read an animated counter's real number from its setting (only plain numbers count).
        if self.counter_depth and tag not in self.VOID:
            self.counter_depth += 1
        number = next((a[k] for k in self.COUNTER_ATTRS if re.fullmatch(r"[\d.,]+", a.get(k) or "")), None)
        # Only real counters: a counter-like class name, or animation settings. (Some pages
        # carry data-start/data-end text positions on ordinary headings; those are not counters.)
        looks_like_counter = (re.search(r"count|stat|number|odometer|\bsn\b", a.get("class") or "")
                              or any(k in a for k in ("data-suffix", "data-prefix", "data-duration")))
        if self.skip_depth == 0 and number and looks_like_counter and not self.counter_depth:
            self.pieces.append(f"\n{a.get('data-prefix') or ''}{number}{a.get('data-suffix') or ''}\n")
            if tag not in self.VOID:
                self.counter_depth = 1

    def handle_endtag(self, tag):
        if self.counter_depth:
            self.counter_depth -= 1
        if tag in self.SKIP and self.skip_depth > 0:
            self.skip_depth -= 1
        elif tag == "title":
            self.in_title = False
        if tag in self.BLOCK:
            self.pieces.append("\n")

    def handle_data(self, data):
        if self.skip_depth > 0:
            return
        if self.in_title:
            self.title += data
        elif self.counter_depth and re.fullmatch(r"\s*[\d.,]*\s*[%+KkMm]*\s*", data):
            return  # the counter's "0%" placeholder; the real number was added above
        else:
            self.pieces.append(data)

    def text(self):
        # Squeeze runs of spaces into one and drop empty lines.
        lines = (" ".join(line.split()) for line in "".join(self.pieces).splitlines())
        return "\n".join(line for line in lines if line)


def page_name(url):
    """Turns a URL into a file name: https://site.com/ -> home, /products/vr -> products_vr."""
    return urlparse(url).path.strip("/").replace("/", "_") or "home"


def charset(content_type):
    """Finds the text encoding a website declared, e.g. 'text/html; charset=UTF-8'."""
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            return part.split("=", 1)[1].strip('"')
    return "utf-8"


# Plain-English meanings for download failures, matched against the stored error
# text, so old receipts get explained too. The first matching entry wins.
ERROR_MEANINGS = [
    (r"Install Certificates", ""),  # certificate_hint already explains this one
    (r"CERTIFICATE_VERIFY_FAILED|certificate", "The site's security certificate could not be checked (it has expired or "
     "does not match the address), so nothing was downloaded. Usually the site's problem, not ours."),
    (r"robots\.txt returned HTTP", "The site refused to share its robots.txt (the file that says what automated "
     "tools may download), so the page was treated as off limits and skipped. Often a bot check; fetch again later."),
    (r"not allowed by robots|robots\.txt disallows", "The site's robots.txt asks automated tools not to download this "
     "page, so it was skipped on purpose. This is respected, not worked around."),
    (r"nodename nor servname|Name or service not known|getaddrinfo|No address associated",
     "The website address does not exist (no such domain). The company may have moved to a new address or closed."),
    (r"timed out|timeout", "The site took too long to answer. Usually temporary; fetch again later."),
    (r"HTTP error (401|403)", "The website refused the download. Usually a bot check (such as Cloudflare) that blocks "
     "automated tools while still letting people open the page. Often temporary; fetch again later."),
    (r"HTTP error (404|410)", "The page no longer exists at this address (moved or deleted). Find its new address, or "
     "remove it from the company's tracked pages."),
    (r"HTTP error 429", "The website asked us to slow down (too many requests). The fetch already waits and retries "
     "once; fetch again later."),
    (r"HTTP error 5\d\d", "The website's own server had a problem. Not caused by us, and usually temporary; fetch again later."),
    (r"Connection (refused|reset)|Remote end closed|EOF occurred", "The connection to the site dropped before the page "
     "arrived. Usually temporary; fetch again later."),
]


def explain_error(error):
    """One plain-English sentence for a failed download, or "" if there is nothing to add."""
    for pattern, meaning in ERROR_MEANINGS:
        if re.search(pattern, str(error or ""), re.I):
            return meaning
    return ""


def with_meaning(error):
    """The error as recorded, followed by its plain-English meaning when there is one."""
    meaning = explain_error(error)
    return f"{error}. Meaning: {meaning}" if meaning else str(error)


def certificate_hint(error):
    """Adds a fix to the one error that comes from this Mac's setup, not the website."""
    if "CERTIFICATE_VERIFY_FAILED" in str(error):
        return (" (If every site fails like this, this Python has no list of trusted certificates: "
                "double-click 'Install Certificates.command' in the Python folder under Applications. "
                "If only this site fails, its own security certificate is expired or invalid.)")
    return ""


_ROBOTS = {}                  # one entry per website, so robots.txt is read once per run
_ROBOTS_LOCK = threading.Lock()


def site_rules(url):
    """Reads (once per run) and returns the site's robots.txt as (rules or None, note).
    None means the whole site is allowed or blocked as the note says."""
    parts = urlparse(url)
    site = f"{parts.scheme}://{parts.netloc}"
    with _ROBOTS_LOCK:
        if site in _ROBOTS:
            return _ROBOTS[site]
    rules = RobotFileParser()
    try:
        with urlopen(Request(site + "/robots.txt", headers={"User-Agent": USER_AGENT}), timeout=TIMEOUT_SECONDS) as r:
            rules.parse(r.read().decode("utf-8", "replace").splitlines())
        result = (rules, "read")
    except HTTPError as e:
        if e.code in (401, 403):
            result = (None, f"robots.txt returned HTTP {e.code}; treated as not allowed")
        else:
            result = (None, f"no robots.txt (HTTP {e.code}); allowed by default")
    except (URLError, OSError) as e:
        result = (None, f"could not read robots.txt: {getattr(e, 'reason', e)}{certificate_hint(e)}")
    with _ROBOTS_LOCK:
        _ROBOTS[site] = result
    return result


def robots_allows(url):
    """Asks the site's robots.txt whether this tool may visit the URL. Returns (yes/no, note)."""
    rules, note = site_rules(url)
    if rules is None:
        return note.startswith("no robots.txt"), note
    if rules.can_fetch(USER_AGENT, url):
        return True, "allowed by robots.txt"
    return False, "not allowed by robots.txt"


def pause_for(url):
    """Seconds to wait between pages on this site: its Crawl-delay if set, else the default."""
    rules, _ = site_rules(url)
    delay = rules.crawl_delay(USER_AGENT) if rules else None
    return float(delay) if delay else SECONDS_BETWEEN_PAGES


def download(url, retry=True):
    """Fetches one URL. Returns (http_status, final_url, content_type, raw_bytes, error).
    If the site answers 429 Too Many Requests, waits as long as it asks (30 s if it
    does not say, at most 60 s) and tries once more."""
    try:
        with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=TIMEOUT_SECONDS) as r:
            return r.status, r.geturl(), r.headers.get("Content-Type", ""), r.read(), None
    except HTTPError as e:
        if e.code == 429 and retry:
            wait = e.headers.get("Retry-After", "")
            time.sleep(min(int(wait), 60) if wait.isdigit() else 30)
            return download(url, retry=False)
        return e.code, url, "", b"", f"HTTP error {e.code} {e.reason}"
    except (URLError, OSError) as e:
        return None, url, "", b"", f"could not connect: {getattr(e, 'reason', e)}{certificate_hint(e)}"


def snapshot_page(competitor, url, folder):
    """Downloads one page and writes its .html, .txt and .receipt.json into folder."""
    name = page_name(url)
    if (folder / f"{name}.receipt.json").exists():
        # Two pages would share a file name (e.g. site.com/ and training.site.com/): add the host.
        name = f"{urlparse(url).netloc.replace('.', '-')}_{name}"
    receipt = {
        "competitor": competitor["name"],
        "requested_url": url,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ok": False,
    }
    allowed, receipt["robots"] = robots_allows(url)

    if not allowed:
        receipt["error"] = "skipped: " + receipt["robots"]
    else:
        status, final_url, content_type, raw, error = download(url)
        receipt.update(http_status=status, final_url=final_url, content_type=content_type, error=error)
        if error is None:
            try:
                html = raw.decode(charset(content_type), errors="replace")
            except LookupError:  # the site declared an encoding Python doesn't know
                html = raw.decode("utf-8", errors="replace")
            parser = TextExtractor()
            parser.feed(html)
            title = " ".join(parser.title.split())
            text = parser.text()

            (folder / f"{name}.html").write_text(html, encoding="utf-8")
            (folder / f"{name}.txt").write_text(
                f"TITLE: {title}\nMETA DESCRIPTION: {parser.meta_description}\n\n{text}\n",
                encoding="utf-8",
            )
            receipt.update(ok=True, title=title, meta_description=parser.meta_description,
                           html_bytes=len(raw), text_characters=len(text))
            if len(text) < SHORT_TEXT_WARNING:
                receipt["warning"] = (f"only {len(text)} characters of text; the page may be built "
                                      "by JavaScript, so compare page.txt with the live site")

    (folder / f"{name}.receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


def load_competitors():
    with open(COMPETITORS_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    competitors = load_competitors()
    if len(sys.argv) > 1:
        wanted = [slug.lower() for slug in sys.argv[1:]]
        unknown = [slug for slug in wanted if slug not in {c["slug"] for c in competitors}]
        if unknown:  # checked before anything is downloaded
            sys.exit(f"No competitor with slug {', '.join(unknown)} in {COMPETITORS_FILE.name}; nothing fetched.")
        competitors = [c for c in competitors if c["slug"] in wanted]

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    # Group the work by website: one worker per site keeps that site's pages in order
    # and pauses between them, while different sites run at the same time.
    by_site = {}
    for competitor in competitors:
        folder = SNAPSHOT_DIR / competitor["slug"] / stamp
        folder.mkdir(parents=True, exist_ok=True)
        site = urlparse(competitor["website"]).netloc
        by_site.setdefault(site, []).extend((competitor, url, folder) for url in competitor["pages"].split())
    print_lock = threading.Lock()

    def fetch_site(jobs):
        failed = 0
        for i, (competitor, url, folder) in enumerate(jobs):
            if i > 0:
                time.sleep(pause_for(url))
            r = snapshot_page(competitor, url, folder)
            with print_lock:
                if r["ok"]:
                    print(f"OK    {competitor['name']}: {url} ({r['text_characters']} characters of text)", flush=True)
                    if "warning" in r:
                        print(f"      WARNING: {r['warning']}", flush=True)
                else:
                    failed += 1
                    print(f"FAIL  {competitor['name']}: {url} -> {with_meaning(r['error'])}", flush=True)
        return failed

    with ThreadPoolExecutor(max_workers=PARALLEL_SITES) as pool:
        failures = sum(pool.map(fetch_site, by_site.values()))

    print(f"\nDone with {failures} failure(s). Snapshots are in data/snapshots/")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
