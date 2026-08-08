"""Server-rendered HTML. Python strings, escaped at every seam.

No template engine and no build step: the pages are functions, the styles
live in one hand-written stylesheet (static/corpus.css), and JavaScript
appears only where the page genuinely moves — the run page's log poll and
the history table's row links. Everything dynamic passes through
`html.escape` on the way in.

The visual language is the Zelcast family — near-black dark theme, one warm
gold accent, tight heavy headings, uppercase eyebrow labels, hairline rules,
square corners — built as a ruled ledger rather than Zelcast's card room, so
the two products read as siblings and never as each other. The dotted-square
meter is the one signature element and it only ever encodes a real
quantity: corpus tier, axis signal, map-slice progress, coverage.
"""

from __future__ import annotations

import html
import re
import sqlite3
import time
from typing import Any

#: The purpose statement the form leads with, verbatim visible text.
DISCLAIMER = (
    "This tool profiles how someone thinks from their public writing. "
    "It is not for candidate screening or employment decisions."
)

#: What each stored status is called where a person reads it.
STATUS_LABELS = {
    "proposed": "estimate ready",
    "queued": "queued",
    "running": "running",
    "done": "done",
    "failed": "failed",
}

_TIER_FILL = {"thin": 1, "moderate": 2, "rich": 3}
_SIGNAL_FILL = {"no": 0, "none": 0, "weak": 1, "strong": 3}

_MAP_LINE = re.compile(r"\[map (\d+)/(\d+)\]")
_SPEND_LINE = re.compile(r"\$(\d+\.\d{2,4})")


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _when(ts: Any) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def _status(value: str) -> str:
    label = STATUS_LABELS.get(value, value)
    return f"<span class='status status-{_e(value)}'>{_e(label)}</span>"


# -- the signature meter ----------------------------------------------------


def _meter(filled: int, total: int, *, live: bool = False, label: str = "") -> str:
    """A row of squares encoding a real quantity. Never decoration."""
    filled = max(0, min(filled, total))
    aria = _e(label or f"{filled} of {total}")
    squares = "".join(
        "<i class='on'></i>" if index < filled else "<i></i>" for index in range(total)
    )
    css = "meter live" if live else "meter"
    text = f"<span class='meter-label small muted'>{_e(label)}</span>" if label else ""
    return f"<span class='{css}' role='img' aria-label='{aria}'>{squares}</span>{text}"


def tier_meter(tier: str, *, labelled: bool = True) -> str:
    """Corpus tier as a three-segment meter: thin, moderate, rich."""
    name = (tier or "").lower()
    if name not in _TIER_FILL:
        return ""
    return _meter(_TIER_FILL[name], 3, label=name if labelled else "")


# -- page shell --------------------------------------------------------------

_NAV = (("/", "New run"), ("/runs", "History"), ("/audit", "Audit log"))


def page(title: str, body: str, *, theme: str = "light", active: str = "") -> str:
    theme_attr = "dark" if theme == "dark" else "light"
    other = "light" if theme_attr == "dark" else "dark"
    links = "".join(
        f"<a href='{href}'"
        + (" aria-current='page'" if href == active else "")
        + f">{_e(label)}</a>"
        for href, label in _NAV
    )
    return (
        "<!doctype html><html lang='en' data-theme='"
        + theme_attr
        + "'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_e(title)}</title>"
        "<link rel='stylesheet' href='/static/corpus.css'></head><body>"
        "<header class='topbar'>"
        "<a class='wordmark' href='/'>Corpus</a>"
        f"<nav>{links}</nav>"
        "<form class='theme' method='post' action='/theme'>"
        f"<input type='hidden' name='to' value='{other}'>"
        f"<button type='submit' class='btn-quiet small'>{other.capitalize()} theme</button>"
        "</form></header>"
        f"<main>{body}</main></body></html>"
    )


def _rule(label: str) -> str:
    return f"<div class='rule'><span class='eyebrow'>{_e(label)}</span></div>"


# -- the new-run form -------------------------------------------------------


def _field(name: str, label: str, flag: str, *, value: str = "", hint: str = "") -> str:
    hint_html = f"<div class='hint'>{_e(hint)}</div>" if hint else ""
    return (
        f"<div class='field'><label for='{name}'>{_e(label)}"
        f"<span class='flag'>{_e(flag)}</span></label>"
        f"<input name='{name}' id='{name}' value='{_e(value)}'>{hint_html}</div>"
    )


def new_run_form(profiles: dict[str, str], error: str = "", theme: str = "light") -> str:
    options = "".join(
        f"<option value='{_e(key)}'>{_e(key)} — {_e(display)}</option>"
        for key, display in sorted(profiles.items())
    )
    error_block = f"<div class='error'>{_e(error)}</div>" if error else ""
    saved = (
        "<div class='field field-wide'><label for='target'>Run a saved target again"
        "<span class='flag'>--target</span></label>"
        f"<select name='target' id='target'><option value=''>— set up a new target —</option>{options}</select>"
        "<div class='hint'>Picking one ignores the fields below; the card on file wins.</div></div>"
        if profiles
        else ""
    )
    body = f"""
<span class='eyebrow'>New run</span>
<h1>Profile a person from<br>their public writing</h1>
<p class='purpose'>{_e(DISCLAIMER)}</p>
{error_block}
<form class='run' method='post' action='/runs'>
  {_rule("Who they are")}
  <div class='fields'>
    {saved}
    {_field("name", "Name", "--name", hint="Required for a new target.")}
    {_field("key", "Key", "--target", hint="Short id for the saved profile and the output folder. Required.")}
    {_field("employer", "Employer", "--employer", hint="Used to score whether a found page is really theirs.")}
    {_field("role", "Role", "--role")}
  </div>
  {_rule("Where to look")}
  <div class='fields'>
    {_field("x", "X handle", "--x")}
    {_field("github", "GitHub username", "--github")}
    {_field("site", "Their site", "--site", hint="Read as an anchor, then crawled for more of theirs.")}
    {_field("bluesky", "Bluesky handle", "--bluesky", hint="e.g. janesmith.bsky.social")}
    {_field("hn", "Hacker News username", "--hn")}
    {_field("reddit", "Reddit username", "--reddit")}
    {_field("mastodon", "Mastodon address", "--mastodon", hint="e.g. @user@mastodon.social")}
  </div>
  {_rule("Limits")}
  <div class='fields'>
    {_field("budget", "Budget in dollars", "--budget", value="10.00", hint="A hard stop, not a target. The run refuses work it cannot pay for.")}
    {_field("highlights", "Highlights cap", "--highlights", value="60", hint="Documents pasted into the final synthesis call — the biggest cost lever.")}
  </div>
  {_rule("Why this run")}
  <div class='field field-wide'>
    <label for='reason'>Reason<span class='flag'>recorded in the audit log</span></label>
    <textarea name='reason' id='reason' rows='2' required></textarea>
  </div>
  <div class='actions'>
    <button type='submit' class='btn-primary'>Run the estimate</button>
    <span class='small muted'>Free. The paid run starts only after you confirm the estimate.</span>
  </div>
</form>
"""
    return page("Corpus — new run", body, theme=theme, active="/")


# -- the estimate receipt ----------------------------------------------------


def proposal_page(run: sqlite3.Row, token: str, theme: str = "light") -> str:
    run_id = int(run["id"])
    body = f"""
<span class='eyebrow'>Estimate ready</span>
<h1>{_e(run["target_key"])}</h1>
<p>This is the CLI's own dry run — the same plan and price a terminal user
would see. <strong>Nothing has been spent.</strong></p>
<div class='receipt'><pre>{_e(run["dry_run_output"])}</pre></div>
<div class='actions'>
  <form method='post' action='/runs/{run_id}/confirm'>
    <input type='hidden' name='token' value='{_e(token)}'>
    <button type='submit' class='btn-primary'>Start the run</button>
  </form>
  <a class='btn btn-quiet' href='/'>Discard this estimate</a>
</div>
<p class='small muted'>Starting the run applies the same budget hard stop a
terminal run gets — the estimate above shows the limit it will refuse to cross.</p>
"""
    return page(f"Corpus — estimate for {run['target_key']}", body, theme=theme)


# -- the run page ------------------------------------------------------------


def _progress_from_lines(lines: list[str]) -> tuple[int, int, str]:
    """(slices done, slices total, last dollar figure seen) from the log."""
    done, total, spend = 0, 0, ""
    for line in lines:
        map_match = _MAP_LINE.search(line)
        if map_match:
            done = max(done, int(map_match.group(1)))
            total = max(total, int(map_match.group(2)))
        for amount in _SPEND_LINE.findall(line):
            spend = amount
    return done, total, spend


def run_page(run: sqlite3.Row, lines: list[sqlite3.Row], theme: str = "light") -> str:
    run_id = int(run["id"])
    status = str(run["status"])
    texts = [str(line["line"]) for line in lines]
    log_text = "\n".join(texts)
    last_seq = int(lines[-1]["seq"]) if lines else 0
    done, total, spend = _progress_from_lines(texts)
    if status == "done" and float(run["spend"] or 0):
        spend = f"{float(run['spend']):.4f}"

    error_block = ""
    if run["error"]:
        error_block = (
            "<div class='error'><span class='eyebrow'>Failed</span><br>"
            f"{_e(run['error'])}<br><span class='small muted'>The full output, "
            "untrimmed, is in the log below.</span></div>"
        )

    links = ""
    if status == "done" and run["out_dir"]:
        links = (
            f"<p class='downloads'><a href='/runs/{run_id}/report'>Read the report</a> · "
            f"<a href='/runs/{run_id}/files/report.md'>report.md</a> · "
            f"<a href='/runs/{run_id}/files/synthesis.json'>synthesis.json</a> · "
            f"<a href='/runs/{run_id}/files/corpus.json'>corpus.json</a> · "
            f"<a href='/runs/{run_id}/files/unconfirmed.md'>unconfirmed.md</a></p>"
        )

    progress = ""
    if total:
        live = status in ("queued", "running")
        progress = (
            "<span id='slices'>"
            + _meter(done, total, live=live, label=f"map {done}/{total}")
            + "</span>"
        )
    spend_html = f"<span class='spend' id='spend'>{'$' + spend if spend else ''}</span>"

    poll = ""
    if status in ("queued", "running"):
        poll = f"""
<script>
let last = {last_seq};
const mapLine = /\\[map (\\d+)\\/(\\d+)\\]/;
const dollars = /\\$(\\d+\\.\\d{{2,4}})/g;
let done = {done}, total = {total};
function meterHtml(d, t) {{
  let squares = '';
  for (let i = 0; i < t; i++) squares += i < d ? "<i class='on'></i>" : '<i></i>';
  return "<span class='meter live' role='img' aria-label='map " + d + " of " + t + "'>" +
         squares + "</span><span class='meter-label small muted'>map " + d + "/" + t + "</span>";
}}
async function poll() {{
  const resp = await fetch('/runs/{run_id}/log?after=' + last);
  const data = await resp.json();
  const log = document.getElementById('log');
  for (const entry of data.lines) {{
    log.textContent += (log.textContent ? '\\n' : '') + entry.line;
    last = entry.seq;
    const m = entry.line.match(mapLine);
    if (m) {{ done = Math.max(done, +m[1]); total = Math.max(total, +m[2]); }}
    let d; while ((d = dollars.exec(entry.line)) !== null) {{
      document.getElementById('spend').textContent = '$' + d[1];
    }}
  }}
  if (total) document.getElementById('slices').innerHTML = meterHtml(done, total);
  if (data.status === 'queued' || data.status === 'running') {{
    setTimeout(poll, 2000);
  }} else {{
    location.reload();
  }}
}}
setTimeout(poll, 2000);
</script>"""

    started = f" · started {_when(run['started_at'])}" if run["started_at"] else ""
    finished = f" · finished {_when(run['finished_at'])}" if run["finished_at"] else ""
    body = f"""
<span class='eyebrow'>Run {run_id}</span>
<h1>{_e(run["target_key"])}</h1>
<div class='runbar'>{_status(status)}{progress}{spend_html}
<span class='small muted'>queued {_when(run["created_at"])}{started}{finished}</span></div>
{error_block}
{links}
<pre class='log' id='log'>{_e(log_text)}</pre>
{poll}
"""
    return page(f"Corpus — run {run_id}", body, theme=theme, active="/runs")


# -- history and audit --------------------------------------------------------


def _history_row(r: sqlite3.Row) -> str:
    run_id = int(r["id"])
    spend = f"${float(r['spend']):.4f}" if r["spend"] else ""
    docs = str(int(r["documents"])) if r["documents"] else ""
    tier = tier_meter(str(r["tier"]))
    report = (
        f"<a href='/runs/{run_id}/report'>report</a>"
        if str(r["status"]) == "done" and r["out_dir"]
        else ""
    )
    return (
        f"<tr class='rowlink' data-href='/runs/{run_id}'>"
        f"<td class='num'><a href='/runs/{run_id}'>{run_id}</a></td>"
        f"<td>{_e(r['target_key'])}</td><td class='num'>{_when(r['created_at'])}</td>"
        f"<td>{tier}</td><td class='num'>{docs}</td><td class='num'>{spend}</td>"
        f"<td>{_status(str(r['status']))}</td><td>{report}</td></tr>"
    )


def history_page(runs: list[sqlite3.Row], theme: str = "light") -> str:
    if not runs:
        body = (
            "<span class='eyebrow'>History</span><h1>No runs yet</h1>"
            "<div class='empty'><p>Every run starts as a free estimate — nothing is "
            "spent until you confirm it.</p>"
            "<p><a class='btn btn-primary' href='/'>Set up the first run</a></p></div>"
        )
        return page("Corpus — history", body, theme=theme, active="/runs")
    rows = "".join(_history_row(r) for r in runs)
    body = (
        "<span class='eyebrow'>History</span><h1>Runs</h1>"
        "<table><thead><tr><th>#</th><th>Target</th><th>Date</th><th>Tier</th>"
        "<th>Docs</th><th>Spend</th><th>Status</th><th></th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "<script>"
        "for (const row of document.querySelectorAll('tr.rowlink')) {"
        "  row.addEventListener('click', (event) => {"
        "    if (event.target.closest('a')) return;"
        "    window.location.href = row.dataset.href;"
        "  });"
        "}"
        "</script>"
    )
    return page("Corpus — history", body, theme=theme, active="/runs")


def audit_page(rows: list[sqlite3.Row], theme: str = "light") -> str:
    if not rows:
        body = (
            "<span class='eyebrow'>Audit log</span><h1>Nothing recorded yet</h1>"
            "<div class='empty'><p>Every run leaves a row here — who ran it, what it "
            "cost, and the reason given. Nothing deletes from this table.</p></div>"
        )
        return page("Corpus — audit log", body, theme=theme, active="/audit")
    cells = "".join(
        f"<tr><td class='num'>{_when(r['ts'])}</td><td>{_e(r['who'])}</td>"
        f"<td>{_e(r['target_key'])}</td><td class='num'>${float(r['spend'] or 0):.4f}</td>"
        f"<td class='num'>{int(r['documents'] or 0) or ''}</td>"
        f"<td>{tier_meter(str(r['tier']))}</td>"
        f"<td>{_status(str(r['status']))}</td><td class='reason'>{_e(r['reason'])}</td></tr>"
        for r in rows
    )
    body = (
        "<span class='eyebrow'>Audit log</span><h1>Who ran what, and why</h1>"
        "<p class='small muted'>One row per run, whatever happened to it. "
        "Nothing deletes from this table.</p>"
        "<table><thead><tr><th>When</th><th>Who</th><th>Target</th><th>Spend</th>"
        "<th>Docs</th><th>Tier</th><th>Status</th><th>Reason</th></tr></thead>"
        f"<tbody>{cells}</tbody></table>"
    )
    return page("Corpus — audit log", body, theme=theme, active="/audit")


# -- the report ---------------------------------------------------------------

_AXIS_P = re.compile(r"<p><strong>([^<]+)</strong> — (strong|weak|no) signal, (\d+) cited document")
_COVERAGE = re.compile(r"Documents analyzed: (\d+) of (\d+)")


def decorate_verdict(html_text: str) -> str:
    """Attach the signal meter to each axis capsule on the verdict page.

    The meter encodes exactly what the sentence already says — none 0, weak
    1, strong 3 of 3 — so a reader can scan the axes before reading them.
    Pattern-matched against render.py's own capsule shape; a capsule that
    does not match renders unchanged rather than wrongly.
    """

    def _inject(match: re.Match[str]) -> str:
        axis, signal, count = match.group(1), match.group(2), match.group(3)
        meter = _meter(
            _SIGNAL_FILL.get(signal, 0),
            3,
            label=f"{signal} signal · {count} cited",
        )
        return (
            f"<div class='axis-meter'><span class='eyebrow'>{axis}</span>{meter}</div>"
            f"<p><strong>{axis}</strong> — {signal} signal, {count} cited document"
        )

    return _AXIS_P.sub(_inject, html_text)


def decorate_evidence(html_text: str) -> str:
    """Turn 'Documents analyzed: X of Y' into the same words plus a fraction bar."""

    def _inject(match: re.Match[str]) -> str:
        analyzed, total = int(match.group(1)), int(match.group(2))
        if not total or analyzed > total:
            return match.group(0)
        percent = round(100 * analyzed / total)
        return (
            match.group(0)
            + f"<span class='fraction' role='img' aria-label='{analyzed} of {total} documents'>"
            + f"<span class='track'><span class='fill' style='width:{percent}%'></span></span>"
            + "</span>"
        )

    return _COVERAGE.sub(_inject, html_text)


def report_page(
    run: sqlite3.Row, part_one_html: str, part_two_html: str, theme: str = "light"
) -> str:
    run_id = int(run["id"])
    tier = tier_meter(str(run["tier"]))
    tier_html = f"<span style='margin-left:0.9rem'>{tier}</span>" if tier else ""
    body = f"""
<div class='prose'>
<span class='eyebrow'>Report · {_e(run["target_key"])}</span>{tier_html}
{decorate_verdict(part_one_html)}
<details class='evidence' id='evidence'><summary>The evidence</summary>
{decorate_evidence(part_two_html)}</details>
<hr>
<p class='downloads'>Downloads: <a href='/runs/{run_id}/files/report.md'>report.md</a> ·
<a href='/runs/{run_id}/files/synthesis.json'>synthesis.json</a> ·
<a href='/runs/{run_id}/files/corpus.json'>corpus.json</a> ·
<a href='/runs/{run_id}/files/unconfirmed.md'>unconfirmed.md</a></p>
</div>
<script>
// Part one links into part two by section anchor; a link into a collapsed
// <details> must open it, or the link silently goes nowhere.
for (const link of document.querySelectorAll("a[href^='#']")) {{
  link.addEventListener('click', () => {{
    document.getElementById('evidence').open = true;
  }});
}}
</script>
"""
    return page(f"Corpus — report for {run['target_key']}", body, theme=theme, active="/runs")
