"""
app.py: the VRp Competitor Hub user interface (Version 0.4).

Start it from the ci-monitor folder with:
    ~/.venvs/ci-monitor/bin/streamlit run app.py

How Streamlit works: every click re-runs this file from top to bottom, and each
st.something(...) line puts one thing on the page. There is no hidden state;
the page always shows what is on disk right now.

This file only lays out screens. It reads data through ui_data.py (read-only)
and its buttons start the existing scripts (fetch.py, verify.py, report.py,
discover.py, review_candidate.py) exactly as Terminal would. It never writes
data files itself; decisions on candidates are saved by review_candidate.py.

Two audiences: on the owner's computer every page works; in the hosted demo
(ui_data.READ_ONLY) visitors see a clean read-only version, and operator
details (file paths, script names, run buttons) are hidden or switched off.
"""

import contextlib
import csv
import html
import io
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime

import streamlit as st

import fetch
import insights
import report
import ui_data

st.set_page_config(page_title="VRp Competitor Hub", layout="wide",
                   page_icon=str(ui_data.PROJECT / "static" / "brand" / "vrp-mark-blue-green-square-512.png"))

# Brand: the official lockup in the sidebar, and the brand kit's own stylesheet
# (brand-tokens.css, copied unchanged) plus ui.css, which only uses its variables.
BRAND = ui_data.PROJECT / "static" / "brand"
st.logo(str(BRAND / "vrp-lockup-green-blue-2000.png"), icon_image=str(BRAND / "vrp-mark-blue-green-square-512.png"), size="large")
st.html("<style>" + (BRAND / "brand-tokens.css").read_text(encoding="utf-8")
        + (BRAND / "ui.css").read_text(encoding="utf-8") + "</style>")

DEMO = ui_data.READ_ONLY

# Tag colors. Each name is a theme color set from the brand tokens in .streamlit/config.toml.
DEGREE_COLORS = {"Self": "green", "1st Degree": "blue", "2nd Degree": "gray", "3rd Degree": "orange"}
LABEL_COLORS = {**DEGREE_COLORS, "Not Relevant": "gray", "Unclear": "red"}
FIELD_STATUS = {"found": ("Found", "green"), "unclear": ("Unclear", "orange"),
                "not_found": ("Not found", "gray"), "UNVERIFIED": ("Check needed", "red")}
# Profile status colors (quality.py decides the status; these only color it).
QUALITY_COLORS = {"Strong": "green", "Partial": "orange", "Needs Review": "red", "Stale": "gray", "Not profiled yet": "gray"}
COMPANY_STATUS_COLORS = {"Current": "green", "Update pending": "blue", "Check needed": "red",
                         "Not profiled yet": "gray", "Website blocks downloads": "gray"}


# ------------------------------------------------------------------ Helpers

def page_header(title, subtitle):
    """The brand header block: Onset panel, white title, Stable accent bar."""
    st.html(f'<div class="vrp-header"><div class="vrp-eyebrow">VRp Competitor Hub</div>'
            f'<p class="vrp-title">{html.escape(title)}</p>'
            f'<p class="vrp-subtitle">{html.escape(subtitle)}</p></div>')


def plain(text):
    """Shows website text exactly as written, so characters like * $ [ ] are not read as formatting."""
    return "".join("\\" + ch if ch in "\\`*_{}[]()#+-.!|<>~$" else ch for ch in str(text))


def when(iso):
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def nice_date(iso):
    """'2026-09-29T18:38:36-06:00' becomes 'Sep 29, 2026'."""
    moment = when(iso)
    return f"{moment:%b} {moment.day}, {moment.year}" if moment else "Not yet"


def days_ago(iso):
    moment = when(iso)
    if not moment:
        return ""
    days = (datetime.now(moment.tzinfo) - moment).days
    return "today" if days < 1 else "yesterday" if days < 2 else f"{days} days ago"


def short_time(iso):
    """'2026-09-23T09:48:50-06:00' becomes '2026-09-23 09:48' (for technical details)."""
    return iso[:16].replace("T", " ") if iso else "never"


def field_label(key):
    return key.replace("_", " ").capitalize()


def link(url, text=None):
    """A markdown link that survives parentheses in the address."""
    safe = url.replace("(", "%28").replace(")", "%29")
    return f"[{plain(text or url)}]({safe})"


def company_status(status):
    """One plain word for where a company's profile stands."""
    fetch = status["fetch"] or {}
    if not status["record_path"]:
        return "Website blocks downloads" if fetch and fetch.get("ok_pages", 0) == 0 else "Not profiled yet"
    if status["unverified"]:
        return "Check needed"
    if status["needs_extraction"]:
        return "Update pending"
    return "Current"


def metric_row(items):
    """Count tiles in one row that wraps: four across on a laptop, two per row on a phone."""
    with st.container(horizontal=True):
        for label, value in items:
            st.metric(label, value, width="stretch")


def table_height(rows):
    """Tall enough to show every row without an inner scrollbar."""
    return 38 + 35 * max(len(rows), 1)


# Result words the scripts print at the start of a line, followed by 2+ spaces.
RESULT_WORD = re.compile(r"^\s*(OK|FAIL|VERIFIED|UNVERIFIED|NOT_FOUND|NOT YET|NEW|CARRIED|EXTRACT|CURRENT|APPROVED|REJECTED|DEFERRED)\s{2,}")


def run_script(args, label, total=None):
    """Runs one of the project's scripts with a progress bar and a plain summary.

    total is how many result lines to expect (e.g. one per page for fetch.py);
    without it a spinner is shown instead. Failures and warnings are always
    listed on the page. The script's full output is kept under "Technical details".
    """
    if DEMO:
        st.info("This is a read-only demo, so nothing can be run here.")
        return
    command = [sys.executable, "-u", *args]  # -u: print each line as it happens
    lines, tally = [], {}
    bar = st.progress(0.0, text=f"{label}: starting") if total else None
    waiting = contextlib.nullcontext() if total else st.spinner(f"{label}...")
    with waiting:
        process = subprocess.Popen(command, cwd=ui_data.PROJECT, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            lines.append(line.rstrip())
            match = RESULT_WORD.match(line)
            if match:
                tally[match.group(1)] = tally.get(match.group(1), 0) + 1
                done = sum(tally.values())
                if bar:
                    bar.progress(min(done / total, 1.0), text=f"{label}: {done} of {total}")
        exit_code = process.wait()
    if bar:
        bar.progress(1.0, text=f"{label}: finished")

    problems = [l.strip() for l in lines if l.strip().startswith(("FAIL", "WARNING", "UNVERIFIED", "- "))]
    if tally:
        st.caption("Results: " + "  ·  ".join(f"{n} {word}" for word, n in tally.items()))
    if exit_code == 0 and not problems:
        st.success(f"{label} finished with no failures or warnings.")
    else:
        st.error(f"{label} finished with {len(problems)} item(s) needing attention:")
        for p in problems:
            st.warning(p)
    with st.expander("Technical details (the script's full output)"):
        st.code("\n".join(lines), language=None)


# ---------------------------------------------------------------- Market overview

def data_status(companies):
    """Per-company data health: degree, website, profile date and status, plus data
    quality notes. For the person who runs the hub (Run the check / How it works)."""
    labels = ui_data.tracked_labels()
    statuses = {c["slug"]: ui_data.company_status(c) for c in companies}
    qualities = ui_data.profile_qualities()
    rows = [{"Company": c["name"], "Degree": [c["type"]], "Website": c["website"],
             "Profile": [qualities[c["slug"]]["status"]],
             "Pages confirmed": nice_date(qualities[c["slug"]].get("confirmed_at")),
             "Status": [company_status(statuses[c["slug"]])],
             "Why": "; ".join(qualities[c["slug"]].get("reasons", [])) if qualities[c["slug"]]["status"] != "Strong" else ""}
            for c in companies]
    st.dataframe(
        rows, hide_index=True, width="stretch", height=table_height(rows),
        column_config={
            "Company": st.column_config.TextColumn("Company", pinned=True),
            "Degree": st.column_config.MultiselectColumn(
                "Degree", options=labels, color=[DEGREE_COLORS.get(l, "gray") for l in labels]),
            "Website": st.column_config.LinkColumn("Website", display_text=r"https?://(?:www\.)?([^/]+)"),
            "Profile": st.column_config.MultiselectColumn(
                "Profile", width="small", options=list(QUALITY_COLORS), color=list(QUALITY_COLORS.values()),
                help="Strong, Partial, Needs Review or Stale (rules in quality.py)"),
            "Status": st.column_config.MultiselectColumn(
                "Status", width="medium", options=list(COMPANY_STATUS_COLORS),
                color=list(COMPANY_STATUS_COLORS.values())),
            "Why": st.column_config.TextColumn("Why", width="large"),
        })
    notes = []
    for c in companies:
        s = statuses[c["slug"]]
        fetch_info = s["fetch"] or {}
        notes += [f"**{plain(c['name'])}**: a page could not be downloaded ({plain(line)})" for line in fetch_info.get("failures", [])]
        notes += [f"**{plain(c['name'])}**: {plain(line)}" for line in fetch_info.get("warnings", [])]
        if s["unverified"]:
            notes.append(f"**{plain(c['name'])}**: quote check failed for {', '.join(s['unverified'])}")
    if notes:
        with st.expander(f"Data quality notes ({len(notes)})"):
            st.caption("Some websites block automated downloads or have pages that no longer exist. "
                       "Those pages are left out of the profiles; nothing is filled in by guesswork.")
            st.markdown("\n".join(f"- {n}" for n in notes))


def snapshot_table(rows, competitors):
    """One market-snapshot table: a bar per topic, the companies behind it, and VRpatients."""
    total = len(competitors)
    data = [{"Topic": label,
             "Competitors": len([n for n in names if n in competitors]),
             "VRpatients": "Yes" if "VRpatients" in names else "No",
             "Which competitors": ", ".join(n for n in names if n in competitors)} for label, names in rows]
    st.dataframe(
        data, hide_index=True, width="stretch", height=table_height(data),
        column_config={
            "Topic": st.column_config.TextColumn("Topic", pinned=True),
            "Competitors": st.column_config.ProgressColumn(
                "Competitors", format=f"%d of {total}", min_value=0, max_value=max(total, 1), width="medium"),
            "VRpatients": st.column_config.TextColumn("VRpatients", width="small"),
            "Which competitors": st.column_config.TextColumn("Which competitors", width="large"),
        })


def page_overview():
    page_header("Market overview", "Who is in the market, what changed in the latest check, and what competitors say they offer.")
    companies = ui_data.companies()
    labels = ui_data.tracked_labels()
    by_name = {c["name"]: c for c in companies}
    metric_row([(label, sum(c["type"] == label for c in companies)) for label in labels])
    if not DEMO:
        waiting = sum(c["status"] in ("new", "deferred") for c in ui_data.candidate_list())
        if waiting:
            st.caption(f"{waiting} discovery candidate(s) are waiting for your review on the Discovery page.")

    day, items, unchanged = ui_data.whats_new()
    st.subheader("What's new")
    st.caption(f"Since the full check on {nice_date(day)}, compared with each company's previous profile. Only clear "
               "signals are listed (platforms, tagline, pricing, fields appearing or disappearing, and the "
               "re-extraction note); re-worded descriptions are ignored.")
    added = [i["company"] for i in items if i["kind"] == "added"]
    changed = [i for i in items if i["kind"] == "changed"]
    cards = [("changed", item) for item in changed] + ([("added", added)] if added else [])
    if cards:
        columns = st.columns(min(3, len(cards)))  # three across on a laptop; stacks on a phone
        for i, (kind_of_card, card) in enumerate(cards):
            with columns[i % len(columns)].container(border=True):
                if kind_of_card == "changed":
                    with st.container(horizontal=True, vertical_alignment="center"):
                        st.markdown(f"**{plain(card['company'])}**", width="content")
                        kind = by_name.get(card["company"], {}).get("type", "")
                        if kind:
                            st.badge(kind, color=DEGREE_COLORS.get(kind, "gray"))
                    for detail in card["details"]:
                        st.caption(plain(detail))
                else:
                    st.markdown(f"**New profiles ({len(card)})**")
                    st.caption(plain(", ".join(f"{name} ({by_name[name]['type']})" if name in by_name else name
                                               for name in card)))
    if not items:
        st.caption("No changes in the latest check.")
    if unchanged:
        st.caption(f"{len(unchanged)} other profiles are unchanged since their previous check.")

    statuses = [q["status"] for q in ui_data.profile_qualities().values()]
    st.caption("Profiles: " + "  ·  ".join(f"{statuses.count(s)} {s}" for s in
               ("Strong", "Partial", "Needs Review", "Stale", "Not profiled yet") if statuses.count(s))
               + ". Open Competitor profiles for why each one has its status.")
    st.subheader("Market snapshot")
    competitor_labels = [label for label in labels if label != "Self"]
    types = st.pills("Compare across", competitor_labels, selection_mode="multi",
                     default=["1st Degree"], key="snapshot-types") or []
    profiles, platform_rows, capability_rows, pricing_rows = ui_data.market_snapshot(types)
    competitors = [name for name, (kind, _) in profiles.items() if kind != "Self"]
    st.caption(f"Across {len(competitors)} competitors with a profile. Every count comes from what the "
               "companies' own pages say (open Competitor profiles for the quotes). VRpatients is shown "
               "for reference and is not counted. Capabilities are matched in the companies' quoted wording; "
               "the Insights page shows the quotes behind each count.")
    if not competitors:
        st.caption("Choose at least one degree.")
        return
    st.markdown("**Platforms learners use**")
    snapshot_table(platform_rows, competitors)
    st.markdown("**Capabilities mentioned**")
    snapshot_table(capability_rows, competitors)
    st.markdown("**Pricing**")
    snapshot_table(pricing_rows, competitors)


# ----------------------------------------------------------------- Insights

MARKET_LABELS = ["Common", "In between", "Uncommon", "None in this set"]
MARKET_COLORS = ["blue", "gray", "orange", "gray"]
VS_GROUPS = [
    ("Only VRpatients", "VRpatients claims it and no competitor in this set has a matching quote."),
    ("Uncommon, VRpatients claims it", "VRpatients claims it, and 2 or fewer competitors do."),
    ("In between, VRpatients claims it", "VRpatients claims it, and more than 2 but fewer than half of competitors do."),
    ("Common, VRpatients claims it", "VRpatients claims it, and at least half of competitors do."),
    ("Competitors claim it, VRpatients does not", "At least one competitor claims it; no matching quote on VRpatients' own pages."),
]


def csv_text(rows):
    """CSV for a download button (opens cleanly in Excel)."""
    buffer = io.StringIO()
    if rows:
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return "﻿" + buffer.getvalue()


def insight_table(rows, key, total, first_column="Topic", show_market=True):
    """A count table with one selectable row per topic or term. Returns the picked row or None."""
    data = [{first_column: r["label"], "Competitors": r["count"],
             **({"Market": [r["market"]]} if show_market else {}),
             "VRpatients": "Yes" if r["vrpatients"] else "No",
             "Which competitors": ", ".join(r["companies"])} for r in rows]
    event = st.dataframe(
        data, hide_index=True, width="stretch", height=table_height(data), key=key,
        on_select="rerun", selection_mode="single-row",
        column_config={
            first_column: st.column_config.TextColumn(first_column, pinned=True),
            "Competitors": st.column_config.ProgressColumn(
                "Competitors", format=f"%d of {total}", min_value=0, max_value=max(total, 1), width="medium"),
            "Market": st.column_config.MultiselectColumn("Market", options=MARKET_LABELS, color=MARKET_COLORS, width="medium"),
            "VRpatients": st.column_config.TextColumn("VRpatients", width="small"),
            "Which competitors": st.column_config.TextColumn("Which competitors", width="large"),
        })
    picked = event.selection.rows
    return rows[picked[0]] if picked else None


def insight_evidence(row, companies, key):
    """The companies and the exact quotes behind one row, VRpatients first."""
    by_name = {c["name"]: c for c in companies}
    st.markdown(f"**{plain(row['label'])}: the quotes behind this row**")
    if row.get("definition"):
        st.caption(plain(row["definition"]))
    names = sorted(row["hits"], key=lambda n: (by_name[n]["type"] != "Self", n))
    if not names:
        st.caption("No company in this set has a matching quote.")
    for name in names:
        company = by_name[name]
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown(f"**{plain(name)}**", width="content")
                st.badge(company["type"] if company["type"] != "Self" else "VRpatients",
                         color=DEGREE_COLORS.get(company["type"], "gray"))
                if company["thin"]:
                    st.badge(f"Only {company['pages_read']} page{'s' if company['pages_read'] != 1 else ''} read", color="orange")
                if st.button("Open profile", key=f"{key}-open-{company['slug']}", type="tertiary"):
                    st.session_state["profile-company"] = name
                    st.switch_page(PROFILES_PAGE)
            for q in row["hits"][name]:
                st.markdown(f"> {plain(q['quote'])}")
                st.caption(f"{field_label(q['field'])}  ·  "
                           + (link(q["source_url"]) if q["source_url"] else "no source address"))


def insight_section(rows, companies, total, key, section, first_column="Topic", show_market=True):
    """Table, download and evidence for one set of rows."""
    picked = insight_table(rows, key, total, first_column, show_market)
    st.download_button("Download these rows with their quotes (CSV)",
                       csv_text(insights.evidence_rows(section, rows, companies)),
                       file_name=f"insights-{key}.csv", mime="text/csv", key=f"{key}-download", type="tertiary")
    if picked:
        insight_evidence(picked, companies, key)
    else:
        st.caption("Select a row to see the companies and the quotes behind it.")


def page_insights():
    page_header("Insights", "What competitors claim, counted from the quotes on their own websites.")
    labels = [label for label in ui_data.tracked_labels() if label != "Self"]
    types = st.pills("Compare across", labels, selection_mode="multi", default=["1st Degree"], key="insight-types") or []
    leave_out_thin = st.toggle("Leave out thin profiles (2 or fewer pages read)", key="insight-thin")
    if not types:
        st.caption("Choose at least one degree.")
        return
    companies = insights.corpus(types, leave_out_thin=leave_out_thin)
    competitors = [c for c in companies if c["type"] != "Self"]
    total = len(competitors)
    if not total:
        st.caption("No competitor profiles in this set.")
        return
    pages = [c["pages_read"] for c in competitors]
    thin = [c["name"] for c in competitors if c["thin"]]
    st.caption(f"{total} competitors with a profile ({', '.join(types)}), plus VRpatients for reference. "
               f"Pages read per company: {min(pages)} to {max(pages)}."
               + (f" Thin profiles: {', '.join(thin)}." if thin else ""))
    with st.expander("How to read this page"):
        t = insights.settings()["thresholds"]
        st.markdown(
            "**From the records** (counts):\n"
            "- A company counts for a row when at least one of its verified quotes matches the row's wording. "
            "Quotes are the companies' exact words, checked against the saved pages. Summaries written during "
            "extraction are not used.\n"
            "- Counts are companies, shown as *X of N*, never percentages: with this few companies, one company "
            "moves a percentage a lot.\n"
            "- VRpatients is matched the same way, from vrpatients.com only, and is not counted in *N*.\n\n"
            "**How to read the labels** (fixed rules, not judgment):\n"
            f"- *Common*: at least {t['common_at_least_share']:.0%} of the competitors in the set. "
            f"*Uncommon*: {t['uncommon_at_most_companies']} or fewer. *In between*: everything else.\n"
            "- *Only VRpatients*: VRpatients has a matching quote and no competitor in this set does.\n\n"
            "**Limits**:\n"
            "- No match means **not claimed on the pages read**, not \"does not offer\". Profiles cover 1 to 10 "
            "pages and list at most 10 capabilities, so counts can be low.\n"
            "- These are the companies' own marketing claims, not tested facts.\n"
            "- Deciding which wording belongs to a topic is a judgment. The lists, and quotes excluded by hand "
            "with a reason, are in config/insights.json. Select a row to check the quotes behind it.\n"
            "- There is no trend yet: this page shows the newest profile of each company only.")
    cap_rows = insights.topic_rows("capabilities", companies)
    msg_rows = insights.topic_rows("messaging", companies)
    caps, msgs, terms, vs = st.tabs(["Capabilities", "Messaging themes", "Terminology", "VRpatients vs market"])
    with caps:
        st.caption("Product capabilities the companies claim, matched in quotes from every field of their profile.")
        insight_section(cap_rows, companies, total, "ins-cap", "Capability")
    with msgs:
        st.caption("Themes in each company's main promise, matched only in the quotes behind its value "
                   "proposition and tagline.")
        insight_section(msg_rows, companies, total, "ins-msg", "Messaging theme")
    with terms:
        st.caption("The words companies use for the same things, counted in every quote of their profile.")
        groups = insights.terminology_rows(companies)
        group = st.selectbox("Word group", list(groups), key="ins-term-group")
        insight_section(groups[group], companies, total, f"ins-term-{list(groups).index(group)}",
                        f"Terminology: {group}", first_column="Term", show_market=False)
    with vs:
        st.caption("Each topic sorted by whether VRpatients' own pages claim it and how many competitors do. "
                   "The labels follow the fixed rules under How to read this page.")
        kind = st.segmented_control("Compare", ["Capabilities", "Messaging themes"], default="Capabilities",
                                    key="ins-vs-kind") or "Capabilities"
        rows = cap_rows if kind == "Capabilities" else msg_rows
        for label, meaning in VS_GROUPS:
            group_rows = [r for r in rows if r["vs_vrpatients"] == label]
            with st.container(border=True):
                st.markdown(f"**{label}** ({len(group_rows)})")
                st.caption(meaning)
                for r in group_rows:
                    st.markdown(f"- {plain(r['label'])}: {r['count']} of {total} competitors"
                                + (f" ({plain(', '.join(r['companies']))})" if 0 < r["count"] <= 3 else ""))
                if not group_rows:
                    st.caption("None.")
        nobody = [r["label"] for r in rows if r["vs_vrpatients"] == "Nobody in this set claims it"]
        if nobody:
            st.caption("No company in this set claims: " + ", ".join(nobody) + ".")
        st.caption("For the quotes behind any line, select the same row on the Capabilities or Messaging themes tab.")


# --------------------------------------------------------------- Comparison

def as_markdown(value):
    """Turns report.py's "- item" lines into a bulleted list; other text is shown as written."""
    lines = value.splitlines()
    if lines and all(line.startswith("- ") for line in lines):
        return "\n".join("- " + plain(line[2:]) for line in lines)
    return plain(value)


def cell(row, key):
    """One table cell: the value, labeled when it is not a plain 'found'."""
    status, value = row[f"{key}_status"], as_markdown(row[key])
    if status == "not_found":
        return ":red-badge[No official tagline found]" if key == "tagline" else "Not found on the pages checked"
    if status == "unclear":
        return f"*Unclear:*\n\n{value}"
    if status == "UNVERIFIED":
        return f"**Check needed:**\n\n{value}"
    return value


def profile_cell(q):
    """The profile status as a colored tag, for the comparison table."""
    if not q:
        return ""
    return f":{QUALITY_COLORS[q['status']]}-badge[{q['status']}]"


def comparison_table(shown):
    quality_by_name = {q["company"]: q for q in ui_data.profile_qualities().values()}
    table = [{"Field": "Degree", **{r["name"]: r["type"] for r in shown}},
             {"Field": "Profile status", **{r["name"]: profile_cell(quality_by_name.get(r["name"])) for r in shown}},
             {"Field": "Pages checked", **{r["name"]: nice_date(r["checked_at"]) for r in shown}}]
    for f in ui_data.field_definitions():
        table.append({"Field": field_label(f["key"]), **{r["name"]: cell(r, f["key"]) for r in shown}})
    st.table(table)


def page_comparison():
    page_header("Comparison", "What each company's own website says, side by side.")
    rows, built_at, stale = ui_data.comparison_table()
    if rows is None:
        st.info("No comparison yet." + ("" if DEMO else " Build it with step 4 on the Run the check page."))
        return
    if stale and not DEMO:
        st.warning("The tracked list or a profile has changed since this comparison was built.")
        if st.button("Rebuild the comparison now", type="primary"):
            run_script(["report.py"], "Building the report")
            st.button("Show the updated table")  # any click re-runs the page with the new file
            return

    profiled = [r for r in rows if r["record"] != "NO RECORD"]
    names = [r["name"] for r in profiled]
    by_name = {r["name"]: r for r in profiled}
    compare, master = st.tabs(["Compare", "Master view"])

    with compare:
        preferred = ["VRpatients", "SimX", "UbiSim", "Oxford Medical Simulation"]
        default = [n for n in preferred if n in names] or names[:4]
        picked = st.multiselect("Companies to compare (up to 5)", names, default=default,
                                max_selections=5, key="compare-picked")
        if picked:
            comparison_table([by_name[n] for n in picked])
        else:
            st.caption("Choose at least one company.")

    with master:
        labels = ui_data.tracked_labels()
        types = st.pills("Degree", labels, selection_mode="multi", default=labels, key="master-types")
        shown = [r for r in profiled if r["type"] in (types or [])]
        st.caption(f"{len(shown)} companies. Scroll sideways to see them all.")
        if shown:
            comparison_table(shown)

    without = [r["name"] for r in rows if r["record"] == "NO RECORD"]
    if without:
        st.caption(f"No profile yet ({len(without)}): " + ", ".join(without)
                   + ". Their websites could not be read automatically.")
    with st.container(horizontal=True):
        output = ui_data.PROJECT / "output"
        for name, label in [("comparison.csv", "Download comparison (CSV)"), ("evidence.csv", "Download evidence (CSV)")]:
            if (output / name).exists():
                st.download_button(label, (output / name).read_bytes(), file_name=f"vrp-{name}",
                                   mime="text/csv", key=f"download-{name}")
    st.caption(f"Built {nice_date(built_at.astimezone().isoformat())}. This table repeats what each company's own "
               "pages say. It does not rank or judge the companies.")


# ----------------------------------------------------- Competitor profiles

def field_card(f, field, result, cause=""):
    key = f["key"]
    shown_status = "UNVERIFIED" if result.get("result") == "unverified" else field.get("status", "not_found")
    text, color = FIELD_STATUS.get(shown_status, ("Not found", "gray"))
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown(f"**{field_label(key)}**", help=f["definition"], width="content")
            st.badge(text, color=color)
        value = field.get("value")
        if isinstance(value, list):
            st.markdown("\n".join(f"- {plain(v)}" for v in value))
        elif value and f.get("display") == "bullets":
            st.markdown(as_markdown(report.as_bullets(value)))
        elif value:
            st.markdown(plain(value))
        else:
            if key == "tagline":
                st.badge("No official tagline found", color="red")
            else:
                st.caption("Not stated on the pages checked.")
        if cause and field.get("status") in ("not_found", "unclear"):
            st.caption(":material/plagiarism: " + cause)
        for problem in result.get("problems", []):
            st.error(problem)
        evidence = result.get("evidence", [])
        if evidence:
            with st.expander(f"Evidence ({len(evidence)} quote{'s' if len(evidence) != 1 else ''})"):
                for e in evidence:
                    st.markdown(f"> {plain(e['quote'])}")
                    source = link(e["source_url"]) if e.get("source_url") else "no source address"
                    st.caption(("Found on the saved page" if e["passed"] else "Not found: " + e["reason"]) + "  ·  " + source)
        if field.get("note"):
            with st.expander("Analyst note"):
                st.caption(plain(field["note"]))


SOURCE_RESULTS = ["Used in profile", "Downloaded, no quotes used", "Downloaded", "Thin text", "Failed", "Not attempted yet"]
SOURCE_COLORS = ["green", "gray", "gray", "orange", "red", "gray"]


def sources_line(cov):
    s = cov["summary"]
    distinct = f" ({s['distinct']} distinct)" if s.get("distinct") is not None and s["distinct"] < s["downloaded"] else ""
    return (f"Sources: {s['configured']} configured  ·  {s['downloaded']} downloaded{distinct}  ·  {s['used']} used as evidence"
            f"  ·  {s['failed']} failed  ·  {s['thin']} thin")


def sources_table(rows, key, evidence=True):
    data = [{"Page": r["url"], "Result": [r["result"]],
             **({"Quotes": r["quotes"], "Fields": ", ".join(field_label(f) for f in r["fields"])} if evidence else {}),
             "Note": r["note"]} for r in rows]
    st.dataframe(data, hide_index=True, width="stretch", height=table_height(data), key=key,
                 column_config={
                     "Page": st.column_config.LinkColumn("Page", display_text=r"https?://(?:www\.)?(.+?)/?$", width="medium"),
                     "Result": st.column_config.MultiselectColumn("Result", options=SOURCE_RESULTS, color=SOURCE_COLORS, width="medium"),
                     "Quotes": st.column_config.NumberColumn("Quotes", width="small"),
                     "Fields": st.column_config.TextColumn("Fields", width="medium"),
                     "Note": st.column_config.TextColumn("Note", width="large"),
                 })


def quality_line(q):
    """One line of profile inputs, in plain words."""
    if q["status"] == "Not profiled yet":
        return "No profile yet."
    age = q["age_days"]
    when = "today" if age == 0 else "1 day ago" if age == 1 else f"{age} days ago"
    check = {"passed": "quote check passed", "failed": "quote check failed", "missing": "no quote check"}[q["check"]]
    return (f"Pages confirmed {nice_date(q['confirmed_at'])} ({when})  ·  extracted {nice_date(q['extracted_at'])}  ·  "
            f"{q['found']} found  ·  {q['unclear']} unclear  ·  {q['not_found']} not stated  ·  "
            f"{q['pages_read']} page{'s' if q['pages_read'] != 1 else ''} read  ·  "
            f"{q['failed_or_weak']} failed or weak page{'s' if q['failed_or_weak'] != 1 else ''}  ·  {check}")


def page_profiles():
    page_header("Competitor profiles", "One company at a time, with the quote and source behind every value.")
    companies = ui_data.companies()
    names = [c["name"] for c in companies]
    company = companies[names.index(st.selectbox("Company", names, key="profile-company"))]
    records = ui_data.record_paths(company["slug"])
    status = ui_data.company_status(company)

    record = ui_data.load_record(records[-1]) if records else {}
    fields = record.get("fields", {})
    results = ui_data.check_record(records[-1]) if records else {}
    q = ui_data.quality.profile_quality(company)
    cov = ui_data.quality.page_coverage(company)

    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.subheader(company["name"], width="content")
            st.badge(company["type"], color=DEGREE_COLORS.get(company["type"], "gray"))
            st.badge(f"Profile: {q['status']}", color=QUALITY_COLORS[q["status"]],
                     help="Strong, Partial, Needs Review or Stale. Rules in quality.py; numbers in config/quality.json.")
            st.space("stretch")
            st.link_button("Visit website", company["website"])
        tagline = fields.get("tagline", {})
        if tagline.get("status") in ("found", "unclear") and tagline.get("value"):
            st.markdown(f"*{plain(tagline['value'])}*")
        facts = [] if records else ["No profile yet"]
        platforms = fields.get("platforms", {}).get("value")
        if isinstance(platforms, list) and platforms:
            facts.append("Platforms: " + " ".join(f":blue-badge[{re.sub(r'[^A-Za-z0-9 ]', '', p)}]" for p in platforms))
        pricing = fields.get("pricing", {})
        if records:
            facts.append("Pricing: " + {"found": "published or on request", "unclear": "unclear"}.get(pricing.get("status"), "not stated"))
        st.markdown("  ·  ".join(facts))
        st.caption(quality_line(q))
        if records:
            st.caption(sources_line(cov))
        if q.get("reasons") and q["status"] != "Not profiled yet":
            st.caption("Why " + q["status"].lower() + ": " + "; ".join(q["reasons"]) + ".")
        for note in q.get("notes", []):
            st.caption("Note: " + note + ".")

    if not records:
        st.info("This company's website could not be read automatically, so it has no profile yet."
                if company_status(status) == "Website blocks downloads"
                else "No profile yet." + ("" if DEMO else " Extract it in Claude Code (see Run the check)."))
        return
    if status["needs_extraction"] and not DEMO:
        st.caption("Newer pages have been saved since this profile was made. Re-extract it if the website changed.")

    with st.expander(f"Sources behind this profile ({cov['summary']['used']} of {cov['summary']['configured']} "
                     f"configured pages used as evidence)"):
        st.caption(f"Pages saved on {nice_date(ui_data.quality.confirmed_at(ui_data.PROJECT / record['snapshot']))}. "
                   "A page with no quotes used is fine: it simply did not state anything the fields needed.")
        sources_table(cov["rows"], "sources-profile")
        if cov["latest"]:
            failed = sum(r["result"] == "Failed" for r in cov["latest_rows"])
            st.markdown(f"**Latest fetch ({nice_date(ui_data.quality.confirmed_at(ui_data.SNAPSHOT_DIR / company['slug'] / cov['latest']) or cov['latest'][:10])})**")
            st.caption(f"{failed} configured page{'s' if failed != 1 else ''} failed in this newer fetch, so the profile "
                       "still uses the pages above." if failed else "A newer fetch exists; the profile has not moved to it yet.")
            sources_table(cov["latest_rows"], "sources-latest", evidence=False)

    definitions = ui_data.field_definitions()
    columns = st.columns(2)
    for i, f in enumerate(definitions):
        with columns[i % 2]:
            status = fields.get(f["key"], {}).get("status")
            cause = ui_data.quality.field_cause(cov, status) if status in ("not_found", "unclear") else ""
            field_card(f, fields.get(f["key"], {}), results.get(f["key"], {}), cause)

    with st.expander("Technical details"):
        checked = ui_data.check_source(records[-1])
        st.caption(f"Record: {records[-1].relative_to(ui_data.PROJECT)}  ·  extracted {short_time(record.get('extracted_at'))} "
                   f"by {record.get('extracted_by', 'unknown')}  ·  from pages saved in {record.get('snapshot')}")
        st.caption(f"Quote checks as recorded on {short_time(checked)}; the saved pages are not included in the demo."
                   if checked else "Quotes re-checked just now against the saved pages.")
        carried = record.get("carried_forward")
        if carried:
            st.caption(f"Carried forward to newer pages on {short_time(carried.get('at'))} ({carried.get('reason')}"
                       + (f"; changed pages: {', '.join(carried['pages_changed'])}" if carried.get("pages_changed") else "")
                       + f"). Originally extracted from {carried.get('from_record')}.")
        if record.get("pages_failed"):
            st.caption("Pages that failed to download for this profile: " + ", ".join(record["pages_failed"]))
        if record.get("note"):
            st.caption("Record note: " + plain(record["note"]))


# ------------------------------------------- How it works / Run the check

# Each step is "script" (the UI can run it) or "manual" (a person does it).
# A future automated extraction would change step 2 to "script" with its own
# command, and nothing else on this page would need to change.
STEPS = [
    {"title": "1. Fetch pages", "kind": "script", "script": "fetch.py", "label": "Fetching pages",
     "about": "Downloads every page in config/competitors.csv into a new dated snapshot. Several websites "
              "are fetched at once, with a 10-second pause between pages on the same site."},
    {"title": "2. Carry forward unchanged profiles", "kind": "script", "script": "carry_forward.py",
     "label": "Checking for changes",
     "about": "Keeps each profile whose quotes are all still on the new pages, so only companies whose "
              "claims changed need a new extraction."},
    {"title": "3. Extract fields", "kind": "manual",
     "about": "Requires a Claude Code session. Open Claude Code in the ci-monitor folder and paste the instruction below."},
    {"title": "4. Verify quotes", "kind": "script", "script": "verify.py", "label": "Checking quotes",
     "about": "Checks every quote in the newest records against the saved page text."},
    {"title": "5. Build report", "kind": "script", "script": "report.py", "label": "Building the report",
     "about": "Rebuilds the comparison and evidence tables from the newest records."},
]


def pick_companies(companies):
    """'Check all that apply': one box for all companies, or one box per company,
    grouped by type. Returns the chosen companies (all of them if 'All' is ticked)."""
    if st.checkbox("All companies", value=True, key="fetch-all"):
        return companies
    labels = ui_data.tracked_labels()
    groups = [(label, [c for c in companies if c["type"] == label]) for label in labels]
    groups.append(("Other", [c for c in companies if c["type"] not in labels]))
    chosen = []
    for label, group in groups:
        if not group:
            continue
        st.markdown(f"**{label}**")
        columns = st.columns(3)
        for i, c in enumerate(group):
            if columns[i % 3].checkbox(c["name"], key=f"fetch-{c['slug']}"):
                chosen.append(c)
    return chosen


def pipeline_stats(companies):
    pages = quotes = profiles = 0
    for c in companies:
        fetch = ui_data.latest_fetch(c["slug"]) or {}
        pages += fetch.get("ok_pages", 0)
        records = ui_data.record_paths(c["slug"])
        if records:
            profiles += 1
            results = ui_data.check_record(records[-1])
            quotes += sum(e["passed"] for r in results.values() for e in r.get("evidence", []))
    return pages, profiles, quotes


def page_how_it_works():
    page_header("How it works", "Four steps turn public websites into a verified comparison.")
    companies = ui_data.companies()
    pages, profiles, quotes = pipeline_stats(companies)
    steps = [
        ("Step 1", "Fetch", f"{pages} pages saved",
         "Python downloads each company's public pages, checking the site's robots.txt rules first, "
         "and keeps a dated receipt for every page."),
        ("Step 2", "Extract", f"{profiles} company profiles",
         "A Claude Code session reads the saved pages and fills seven fields, quoting the exact words "
         "behind each value. Missing information is recorded as not found, never guessed."),
        ("Step 3", "Verify", f"{quotes} quotes verified",
         "Python checks that every quote appears word for word on the saved page. "
         "Anything that fails is flagged instead of shown as fact."),
        ("Step 4", "Report", f"{len(ui_data.field_keys())} fields compared",
         "The comparison and evidence tables are rebuilt from the verified profiles, "
         "with a source link for every quote."),
    ]
    for column, (step, name, stat, text) in zip(st.columns(4), steps):
        with column.container(border=True, height="stretch"):
            st.caption(step.upper())
            st.subheader(name)
            st.markdown(f"**{stat}**")
            st.caption(text)
    st.subheader("What stays with people")
    st.markdown("- Deciding which companies to track, and how to label them\n"
                "- Approving or rejecting every possible new competitor found by Discovery\n"
                "- Any conclusion about strategy. The hub reports what websites say; it does not rank companies.")
    st.caption("Public information only. No paid databases, no private data, and no AI-generated values without a quote behind them.")
    with st.expander("Data status by company"):
        data_status(companies)


def extraction_list(companies, statuses):
    """Which companies need Claude Code, and which carried-forward profiles are worth a look."""
    waiting = [c for c in companies if statuses[c["slug"]]["needs_extraction"]]
    if waiting:
        st.write(f"{len(waiting)} compan{'y needs' if len(waiting) == 1 else 'ies need'} a new extraction: "
                 + ", ".join(c["name"] for c in waiting) + ". Open Claude Code in the ci-monitor folder and paste:")
        st.code("Follow EXTRACTION.md for each of these companies, then run verify.py: "
                + ", ".join(c["slug"] for c in waiting), language=None)
    else:
        st.success("No extraction needed: every company's newest pages already have a profile.")
    changed = [c for c in companies if statuses[c["slug"]]["pages_changed"] and not statuses[c["slug"]]["needs_extraction"]]
    if changed:
        st.caption("Carried forward with changed pages, worth a look for new claims: "
                   + "; ".join(f"{c['name']} ({', '.join(statuses[c['slug']]['pages_changed'])})" for c in changed))


def page_run():
    page_header("Run the check", "Fetch pages, check quotes and rebuild the report. Extraction happens in Claude Code.")
    companies = ui_data.companies()
    statuses = {c["slug"]: ui_data.company_status(c) for c in companies}

    with st.container(border=True):
        st.subheader("Refresh everything")
        st.caption("Runs steps 1, 2, 4 and 5 in order: fetches every company, carries forward profiles whose "
                   "quotes are still on the pages, checks every quote and rebuilds the comparison. Afterwards "
                   "only the companies whose claims changed need Claude Code.")
        if st.button("Refresh everything", type="primary", key="refresh-all"):
            fields = len(ui_data.field_keys())
            run_script(["fetch.py"], "Fetching pages", sum(len(c["pages"].split()) for c in companies))
            run_script(["carry_forward.py"], "Checking for changes",
                       sum(bool(ui_data.record_paths(c["slug"])) for c in companies))
            run_script(["verify.py"], "Checking quotes",
                       sum(bool(ui_data.record_paths(c["slug"])) for c in companies) * fields)
            run_script(["report.py"], "Building the report")
            st.markdown("**What needs Claude Code now**")
            extraction_list(companies, {c["slug"]: ui_data.company_status(c) for c in companies})

    for step in STEPS:
        with st.container(border=True):
            st.subheader(step["title"])
            st.caption(step["about"])

            if step["kind"] == "manual":
                st.badge("Manual: requires a Claude Code session", color="orange")
                extraction_list(companies, statuses)
                continue

            args, total = [step["script"]], None
            if step["script"] == "verify.py":
                total = sum(bool(ui_data.record_paths(c["slug"])) for c in companies) * len(ui_data.field_keys())
            nothing_chosen = False
            if step["script"] == "fetch.py":
                chosen = pick_companies(companies)
                pages = sum(len(c["pages"].split()) for c in chosen)
                # Sites run in parallel; each site pauses between its own pages.
                per_site = [(len(c["pages"].split()) - 1) * fetch.SECONDS_BETWEEN_PAGES for c in chosen] or [0]
                minutes = (max(max(per_site), sum(per_site) / fetch.PARALLEL_SITES) + pages) / 60
                if chosen:
                    st.caption(f"{len(chosen)} compan{'y' if len(chosen) == 1 else 'ies'}, {pages} pages, "
                               f"about {max(minutes, 1):.0f} minute(s).")
                else:
                    st.caption("Tick at least one company.")
                nothing_chosen = not chosen
                total = pages
                if chosen is not companies:
                    args += [c["slug"] for c in chosen]
            if st.button(f"Run {step['script']}", key=step["script"], disabled=nothing_chosen):
                run_script(args, step["label"], total)

    st.subheader("Data status")
    data_status(companies)


# ---------------------------------------------------------------- Discovery

# Same idea as STEPS: "script" steps run here, "manual" steps need Claude Code.
DISCOVERY_STEPS = [
    {"title": "1. Collect news leads", "kind": "script", "args": ["discover.py", "news"], "label": "Collecting news leads",
     "about": "Reads the news feeds in config/discovery.json and saves new articles as leads.", "run": True},
    {"title": "2. Collect award leads", "kind": "script", "args": ["discover.py", "awards"], "label": "Collecting award leads",
     "about": "Reads recent small-business research awards (NIH, NSF) that match the market keywords.", "run": True},
    {"title": "3. Search the web and propose classifications", "kind": "manual",
     "about": "Requires a Claude Code session. It runs the searches, logs every result, turns leads into "
              "candidates, and proposes a degree for each one with quotes from its own website."},
    {"title": "4. Check classifications", "kind": "script", "args": ["discover.py", "check", "--save"],
     "label": "Checking classifications",
     "about": "Checks that each proposed classification uses an allowed label, gives a reason, "
              "and quotes text that really appears on the candidate's saved pages."},
]
# What each proposed label means you should do. A degree means "approve as that degree".
NON_DEGREE_ACTIONS = {"Not Relevant": "Reject", "Unclear": "Check by hand before deciding"}


def recommended_action(proposed):
    labels = [label for label in ui_data.tracked_labels() if label != "Self"]
    if proposed in labels:
        return f"Approve as {proposed}"
    return NON_DEGREE_ACTIONS.get(proposed, "No recommendation yet")


def decision_text(c):
    review = c.get("review") or {}
    if c["status"] == "approved":
        return f"Added as {review.get('type', '')}".strip()
    return {"rejected": "Rejected", "deferred": "Deferred", "new": "Awaiting review"}.get(c["status"], c["status"])


ORIGIN_LABELS = {"topic-search": "Topic search", "news": "News", "award": "Award record",
                 "seed-list": "Earlier research list", "lookup": "Name lookup"}
RUN_NAME = datetime.now().strftime("%Y-%m-%d")
ORIGIN_OF = {}


def found_by(c):
    """Where the candidate's name first came from (see DISCOVERY.md, origin)."""
    if c["name"] not in ORIGIN_OF:  # filled once per page load in page_discovery
        ORIGIN_OF.update({b["name"]: b["origin"] for b in ui_data.discovery_baseline()})
    return ORIGIN_LABELS.get(ORIGIN_OF.get(c["name"], ""), "Unknown")


def discovery_quality():
    """Discovery quality numbers: per run, by origin and by query, plus the baseline."""
    reports = ui_data.discovery_reports()
    st.markdown("The question: does discovery find relevant companies that are **new to us** (not tracked, not an "
                "earlier candidate, not in the seed lists)? **New and approved** is the number that matters.")
    def row(label, t):
        return {"": label, "Results looked at": t["considered"],
                "Already known": f"{t['already_known']} ({t['already_known_share']:.0%})" if t["already_known_share"] is not None else "0",
                "Ignored sites": t["ignored"], "New to us": t["new"], "Proposed relevant": t["new_proposed_relevant"],
                "Proposed not relevant": t["new_proposed_not_relevant"], "Waiting for review": t["new_needs_review"],
                "New and approved": t["new_relevant_approved"]}
    if reports:
        st.markdown("**Runs**")
        runs = [row(r["run"], r["overall"]) for r in reports]
        st.dataframe(runs, hide_index=True, width="stretch", height=table_height(runs))
        pick = st.selectbox("Break down a run", [r["run"] for r in reports], key="disc-run")
        rep = next(r for r in reports if r["run"] == pick)
        st.caption(f"{rep['leads']} lead(s) collected in this run.")
        for title, part in (("By where the name came from", "by_origin"), ("By source and query", "by_query")):
            st.markdown(f"**{title}**")
            rows = [row(ORIGIN_LABELS.get(k, k) if part == "by_origin" else k, v)
                    for k, v in sorted(rep[part].items(), key=lambda kv: -kv[1]["considered"])]
            st.dataframe(rows, hide_index=True, width="stretch", height=table_height(rows))
    else:
        st.caption("No logged runs yet. Runs are logged from the first discovery session that uses run names.")
    st.markdown("**Before run logs (baseline)**")
    st.caption("Candidates saved before 2026-10-14, by where each one's first sighting came from. Results that "
               "were already tracked or ignored were not kept then, so the known share cannot be computed.")
    base = ui_data.discovery_baseline()
    rows = []
    for origin, label in ORIGIN_LABELS.items():
        group = [b for b in base if b["origin"] == origin]
        if group:
            rows.append({"Where the name came from": label, "Candidates": len(group),
                         "Approved": sum(b["approved"] is not None for b in group),
                         "Companies": ", ".join(b["name"] for b in group)})
    st.dataframe(rows, hide_index=True, width="stretch", height=table_height(rows),
                 column_config={"Companies": st.column_config.TextColumn("Companies", width="large")})


def show_candidate(c):
    with st.container(horizontal=True, vertical_alignment="center"):
        st.subheader(c["name"], width="content")
        st.space("stretch")
        st.link_button("Visit website", c["website"])
    st.caption(f"First seen {nice_date(c['first_seen'])}  ·  found by {found_by(c).lower()}  ·  {decision_text(c)}")
    if c["aliases"]:
        st.caption("Also seen as: " + plain(", ".join(c["aliases"])))
    if not DEMO:
        if c["possible_duplicate_of"]:
            st.warning("Name looks like " + ", ".join(c["possible_duplicate_of"]) + ". Check it is not a duplicate.")
        if c.get("name_on_site") is False:
            st.warning(f"'{c['name']}' does not appear on its saved homepage. Check this website belongs to the company.")
        for failure in ui_data.candidate_fetch_failures(c):
            st.warning("Website not saved: " + failure)

    cl = c.get("classification")
    with st.container(border=True):
        st.caption("RECOMMENDATION")
        if not cl:
            st.write("None yet. A Claude Code session recommends a degree with evidence.")
        else:
            problems, results = ui_data.candidate_check(c)
            with st.container(horizontal=True, vertical_alignment="center"):
                st.subheader(recommended_action(cl.get("proposed")), width="content")
                st.badge(cl.get("proposed"), color=LABEL_COLORS.get(cl.get("proposed"), "gray"))
            st.markdown(plain(cl.get("reason", "")))
            st.caption(f"Proposed {nice_date(cl.get('at'))}. A recommendation, not a decision.")
            for p in problems:
                st.error("Quote check failed, so treat this recommendation with care: " + p)
            if results:
                with st.expander(f"Evidence ({len(results)} quote{'s' if len(results) != 1 else ''})"):
                    for e in results:
                        st.markdown(f"> {plain(e['quote'])}")
                        source = link(e["source_url"]) if e.get("source_url") else "no source address"
                        st.caption(("Found on the saved page" if e["passed"] else "Not found: " + e["reason"]) + "  ·  " + source)

    with st.expander(f"How it was found ({len(c['sightings'])} sighting{'s' if len(c['sightings']) != 1 else ''})"):
        for s in c["sightings"]:
            st.markdown(f"**{plain(s.get('source', ''))}**  ·  {nice_date(s.get('found_at'))}"
                        + (f"  ·  search: {plain(s['query'])}" if s.get("query") else ""))
            if s.get("snippet"):
                st.markdown(f"> {plain(s['snippet'])}")
            if s.get("url"):
                st.caption(link(s["url"]))
        if c["signals"] and not DEMO:
            st.caption("Keywords on its saved pages (a sorting aid, not a verdict): " +
                       plain("; ".join(f"{group}: {', '.join(hits)}" for group, hits in c["signals"].items())))

    if c["status"] not in ("new", "deferred"):
        review = c.get("review") or {}
        st.caption(f"Decision: {decision_text(c)} on {nice_date(review.get('at'))}"
                   + (f" ({plain(review['reason'])})" if review.get("reason") else ""))
        if not DEMO and st.button("Undo this decision", key=f"undo-{c['slug']}",
                                  help="Puts the candidate back in the queue. An approval's row is removed from competitors.csv."):
            run_script(["review_candidate.py", "undo", c["slug"]], "Undoing the decision")
        return
    st.caption("Decisions are made on the owner's computer." if DEMO
               else "Decide in the review queue above.")


def review_queue(waiting):
    """One editable table for every candidate awaiting review, saved in one go."""
    labels = [label for label in ui_data.tracked_labels() if label != "Self"]
    options = [f"Approve as {label}" for label in labels] + ["Reject", "Defer"]

    def recommended_option(c):
        proposed = (c.get("classification") or {}).get("proposed")
        return f"Approve as {proposed}" if proposed in labels else "Reject" if proposed == "Not Relevant" else None

    st.subheader("Review queue")
    st.caption(f"{len(waiting)} candidate(s) waiting. Choose a decision for each row, or fill in the "
               "recommendations and change any you disagree with. Unclear candidates are left for you.")
    filled = st.session_state.get("review-filled", False)
    if st.button("Fill in the recommendations", key="review-fill"):
        st.session_state["review-filled"] = filled = True
    labels_all = [cat["label"] for cat in ui_data.discovery_settings()["categories"]]
    rows = [{"Company": c["name"], "Website": c["website"],
             "Recommendation": [(c.get("classification") or {}).get("proposed", "Unclear")],
             "Why": ((c.get("classification") or {}).get("reason") or "")[:160],
             "Decision": recommended_option(c) if filled else None,
             "Reject reason": ""} for c in waiting]
    edited = st.data_editor(
        rows, hide_index=True, width="stretch", key=f"review-editor-{filled}",
        column_order=["Company", "Recommendation", "Decision", "Why", "Reject reason", "Website"],
        disabled=["Company", "Website", "Recommendation", "Why"],
        column_config={
            "Website": st.column_config.LinkColumn("Website", display_text=r"https?://(?:www\.)?([^/]+)"),
            "Recommendation": st.column_config.MultiselectColumn(
                "Recommendation", options=labels_all, color=[LABEL_COLORS.get(l, "gray") for l in labels_all]),
            "Why": st.column_config.TextColumn("Why", width="medium"),
            "Decision": st.column_config.SelectboxColumn("Decision", options=options, width="medium"),
            "Reject reason": st.column_config.TextColumn("Reject reason", help="Optional. Used when you reject."),
        })

    chosen = [(c, row) for c, row in zip(waiting, edited) if row.get("Decision")]
    differs = [c["name"] for c, row in chosen if row["Decision"] != recommended_option(c)]
    confirmed = True
    if differs:
        st.warning("These decisions differ from the recommendation: " + ", ".join(differs) + ".")
        confirmed = st.checkbox("I've checked these", key="review-confirm")
    approve = sum(row["Decision"].startswith("Approve") for _, row in chosen)
    label = (f"Save {len(chosen)} decision(s): {approve} approve, "
             f"{sum(row['Decision'] == 'Reject' for _, row in chosen)} reject, "
             f"{sum(row['Decision'] == 'Defer' for _, row in chosen)} defer") if chosen else "Save decisions"
    if st.button(label, type="primary", disabled=not chosen or not confirmed, key="review-save"):
        decisions = []
        for c, row in chosen:
            proposed = (c.get("classification") or {}).get("proposed", "")
            if row["Decision"].startswith("Approve as "):
                pages = list(dict.fromkeys([c["website"]] + c.get("extra_pages", []) + c.get("suggested_pages", [])))[:5]
                decisions.append({"slug": c["slug"], "decision": "approve", "type": row["Decision"][len("Approve as "):],
                                  "pages": pages, "reason": "Approved in the review queue"})
            elif row["Decision"] == "Reject":
                decisions.append({"slug": c["slug"], "decision": "reject",
                                  "reason": row.get("Reject reason") or f"Rejected in the review queue (recommended: {proposed})"})
            else:
                decisions.append({"slug": c["slug"], "decision": "defer", "reason": row.get("Reject reason") or ""})
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(decisions, f)
        run_script(["review_candidate.py", "batch", f.name], "Saving decisions", len(decisions))
        st.session_state["review-filled"] = False
        st.button("Show the updated queue", key="review-refresh")


def page_discovery():
    page_header("Discovery", "How possible new competitors are found, checked and reviewed.")
    ORIGIN_OF.clear()
    settings = ui_data.discovery_settings()
    candidates = ui_data.candidate_list()
    count = lambda status: sum(c["status"] == status for c in candidates)

    tiles = [("Candidates found", len(candidates)), ("Added to tracking", count("approved")),
             ("Rejected", count("rejected")), ("Awaiting review", count("new") + count("deferred"))]
    metric_row(tiles)
    sources = [found_by(c) for c in candidates]
    st.caption("  ·  ".join(f"{name}: {sources.count(name)}" for name in [*ORIGIN_LABELS.values(), "Unknown"]
                            if sources.count(name)) + ". Every candidate, whatever its source, is classified only "
               "from its own public website.")

    with st.expander("How candidates are found and classified"):
        st.markdown("Web searches and industry news feeds surface possible competitors. Each company's own "
                    "website is saved and checked, and a Claude Code session proposes a label with quotes "
                    "from that site. A person approves or rejects every one.")
        for cat in settings["categories"]:
            st.markdown(f"**{cat['label']}**: {plain(cat['definition'])}")

    with st.expander("Discovery quality: is it finding companies that are new to us?"):
        discovery_quality()

    if not DEMO:
        leads = ui_data.unreviewed_leads()
        with st.expander("Run discovery steps"):
            for step in DISCOVERY_STEPS:
                st.markdown(f"**{step['title']}**")
                st.caption(step["about"])
                if step["kind"] == "manual":
                    unclassified = sum(c["status"] == "new" and not c.get("classification") for c in candidates)
                    st.caption(f"Waiting: {len(leads)} unreviewed lead(s), {unclassified} unclassified candidate(s). "
                               "Open Claude Code in the ci-monitor folder and paste:")
                    st.code(f"Follow DISCOVERY.md. Run name: {RUN_NAME}", language=None)
                elif st.button("Run " + " ".join(step["args"][:2]), key="disc-" + step["args"][1]):
                    args = step["args"] + (["--run", RUN_NAME] if step.get("run") else [])
                    run_script(args, step["label"], len(candidates) if step["args"][1] == "check" else None)
            if leads:
                st.markdown(f"**Unreviewed news leads ({len(leads)})**")
                for lead in leads:
                    st.caption(f"{plain(lead['title'])}  ·  {lead['source']}  ·  {link(lead['link'], 'article')}")

    waiting = [c for c in candidates if c["status"] in ("new", "deferred")]
    if not DEMO and waiting:
        review_queue(waiting)
    st.subheader("All candidates")

    statuses = ["Awaiting review", "Added", "Rejected"]
    group = lambda c: "Added" if c["status"] == "approved" else "Rejected" if c["status"] == "rejected" else "Awaiting review"
    default = ["Awaiting review"] if not DEMO and any(group(c) == "Awaiting review" for c in candidates) else statuses
    chosen = st.pills("Show", statuses, selection_mode="multi", default=default, key="disc-status")
    shown = [c for c in candidates if group(c) in (chosen or [])]
    if not shown:
        st.caption("No candidates match.")
        return
    labels = [cat["label"] for cat in settings["categories"]]
    rows = [{"Company": c["name"], "Website": c["website"],
             "Recommendation": [c["classification"]["proposed"]] if c.get("classification") else [],
             "Decision": decision_text(c), "Found by": found_by(c)} for c in shown]
    st.dataframe(
        rows, hide_index=True, width="stretch", height=table_height(rows),
        column_config={
            "Company": st.column_config.TextColumn("Company", pinned=True),
            "Website": st.column_config.LinkColumn("Website", display_text=r"https?://(?:www\.)?([^/]+)"),
            "Recommendation": st.column_config.MultiselectColumn(
                "Recommendation", options=labels, color=[LABEL_COLORS.get(l, "gray") for l in labels]),
        })
    names = [c["name"] for c in shown]
    pick = st.selectbox("Look at a candidate", names, key="disc-candidate")
    show_candidate(shown[names.index(pick)])


# ------------------------------------------------------------------- Layout

PROFILES_PAGE = st.Page(page_profiles, title="Competitor profiles", url_path="profiles")
pages = {
    "Market": [
        st.Page(page_overview, title="Market overview", url_path="overview", default=True),
        st.Page(page_comparison, title="Comparison", url_path="comparison"),
        PROFILES_PAGE,
        st.Page(page_insights, title="Insights", url_path="insights"),
    ],
    "Method" if DEMO else "Operations": [
        st.Page(page_how_it_works if DEMO else page_run, title="How it works" if DEMO else "Run the check",
                url_path="how-it-works" if DEMO else "run"),
        st.Page(page_discovery, title="Discovery", url_path="discovery"),
    ],
}
navigation = st.navigation(pages)
if DEMO:
    st.sidebar.info(f"Read-only demo. Data as of {nice_date(ui_data.data_as_of())}.")
st.sidebar.caption("Public sources only. Every value is quoted from the company's own website "
                   "and checked word for word against the saved page.")
navigation.run()
