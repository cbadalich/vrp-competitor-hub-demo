# Extraction instructions (Step 2)

These are the instructions a Claude Code session follows to turn saved web
pages into a structured record. A person should be able to read this file and
predict what the session will do. If an extraction looks wrong, check this file
first.

## Input

For the competitor being extracted (identified by its `slug` in
`config/competitors.csv`):

1. Find the newest folder in `data/snapshots/<slug>/` that has at least one
   receipt with `"ok": true`. If newer folders exist where every page failed,
   do not extract from them; tell the user they exist. If the company already
   has a profile, use only a folder where **every page in the profile's
   `pages_read` downloaded**; a fetch where some of them failed (server error,
   block) is not a reason to re-extract (`carry_forward.py` reports it as KEPT).
2. Read every `<page>.receipt.json` in it. Skip pages where `"ok": false`, and
   list them under `pages_failed` in the record.
3. Read every `<page>.txt` file that downloaded successfully. Each file starts with the page's `TITLE:` and
   `META DESCRIPTION:` lines, and both count as page text.
4. Read `config/fields.json` for the fields and their definitions.

## Rules

- **Use only the saved `.txt` files.** Do not browse the web, open the `.html`
  files, or use anything you already know about the company. If it isn't in
  the saved text, it doesn't exist for this record.
- **Every claim needs an exact quote.** Copy the words character for character
  from the `.txt` file, at least 15 characters long. Do not fix typos, join
  sentences from different places, or add "..." inside a quote. A quote may
  run across neighboring lines of the same passage (for example a short
  headline plus the line under it); `verify.py` treats a line break as a space.
- **Duplicate pages.** If two saved pages are identical (a redirect), list both
  in `pages_read`, cite only one, and say so in a top-level `note`.
- **Prefer body text over menus.** Menu labels such as "AI Capabilities" are
  weak evidence. Use them only if nothing better exists, and set the status to
  `unclear`.
- **Do not guess.** If the pages don't answer a field, use `not_found` with a
  `null` value and no evidence. An empty field is a correct answer.
- **Stay close to the company's wording.** Values summarize what the page says.
  Do not judge whether a claim is true, compare against other companies, or
  draw strategic conclusions.
- **Never edit snapshots or earlier records.**

## Status values

| Status | Use when |
|---|---|
| `found` | The page states it directly. |
| `unclear` | The page hints at it, or statements conflict. Explain in `note`. |
| `not_found` | The saved pages do not address it. |

## Field-specific guidance

- `platforms`: how **learners** use the product, using only these labels:
  Web, VR, AR, MR, Mobile, Desktop, Physical hardware, Projection or immersive
  room. "XR" counts as VR. "Mixed Reality" is MR. Physical hardware means
  learners train on physical devices (manikins, task trainers, hardware
  simulators). Projection or immersive room means learners train in front of
  projection or wraparound screens, or in a projection room.
  Web means the pages say it runs in a browser or online. Desktop means an
  installed computer app, or "computer screen" with no mention of a browser.
  Tools used only by educators (editors, facilitator consoles) do not count;
  mention them in `note`.
- `key_capabilities`: a list of short feature phrases, up to 10, each backed by
  evidence. Features only; leave out benefits like "builds confidence".
- `tagline`: a slogan marked ™, ® or ℠, or a slogan that repeats (on two or
  more pages, or in both a page's TITLE or META DESCRIPTION and its body), copied
  exactly. Product names with a mark do not count. If several qualify, prefer
  the marked one, then the one repeated most. If none qualifies, use
  `not_found` with a note that starts "No official tagline found" and lists the
  candidates seen (for example the homepage headline).
- `pricing`: only published prices, plans, or pricing models. A button or line
  that mentions price, pricing, or a quote ("Get Pricing", "Request a quote")
  counts as `found` with the value "Not published; quote on request". Generic
  contact buttons ("Talk to our team", "Book a demo") do not count: use
  `not_found`.

## Output

Write one file: `data/records/<slug>/<snapshot folder name>.json`. The name
matches the snapshot folder so each record points at exactly one set of pages.

```json
{
  "competitor": "UbiSim",
  "slug": "ubisim",
  "snapshot": "data/snapshots/ubisim/2026-09-23_101500",
  "extracted_at": "2026-09-23T10:30:00-06:00",
  "extracted_by": "Claude Code session, model <model name>",
  "pages_read": ["home", "how-it-works"],
  "pages_failed": [],
  "fields": {
    "tagline": {
      "status": "found",
      "value": "Exact headline text",
      "evidence": [{"page": "home", "quote": "Exact headline text"}],
      "note": ""
    },
    "pricing": {
      "status": "not_found",
      "value": null,
      "evidence": [],
      "note": "No pricing on the 10 pages read."
    }
  }
}
```

`page` is the file name without `.txt`. Do not write URLs; `verify.py` looks
them up from the receipts.

## When this company already has a profile

If `data/records/<slug>/` already holds an earlier record, add one top-level
key after writing the new fields:

```json
"changes_since_previous": "One or two sentences on what the WEBSITE now says differently, in substance."
```

Compare against the previous record only after writing yours. Mention only
changes the new quotes show (a feature now live, a platform dropped, a new
price). If nothing changed in substance, write "No substantive change". If a
value differs only because of a judgment call (for example a different
tagline candidate), say so. The Market overview page shows this note.

## Revising a record without new pages

When a rule changes and a profile has to be redone from the same saved pages,
do not edit the existing record. Write `<snapshot folder name>_r2.json` (then
`_r3`, and so on) next to it, with a top-level `"revision"` block
(`from_record`, `at`, `reason`, `changed`) and a `changes_since_previous` note
that starts "No substantive website change" when only the rule changed.

## After writing the record

Run `python3 verify.py <slug>`. If any field is UNVERIFIED, fix the quote so it
matches the page exactly, or downgrade the field to `unclear` or `not_found`.
Do not remove a failing quote just to make the check pass while keeping the
same value. Report the final verify output to the user.
