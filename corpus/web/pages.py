"""Server-rendered HTML. Python strings, escaped at every seam.

No template engine and no build step: the pages are functions, the CSS is
one block, and the only JavaScript is the run page's poll loop. Everything
dynamic passes through `html.escape` on the way in.
"""

from __future__ import annotations

import html
import sqlite3
import time
from typing import Any

_CSS = """
body { font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 60rem;
       padding: 0 1rem; color: #1a1a1a; }
a { color: #0b5cad; }
h1, h2, h3 { line-height: 1.25; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #ddd;
         vertical-align: top; }
form.run label { display: block; margin: .7rem 0 .15rem; font-weight: 600; }
form.run input, form.run select, form.run textarea { width: 100%; padding: .4rem;
         box-sizing: border-box; font: inherit; }
form.run .row { display: grid; grid-template-columns: 1fr 1fr; gap: 0 1.5rem; }
.notice { background: #fff7e0; border: 1px solid #e0c060; padding: .8rem 1rem;
          margin: 1rem 0; }
.error { background: #fde8e8; border: 1px solid #d08080; padding: .8rem 1rem; }
pre.log { background: #14181c; color: #d6dde4; padding: 1rem; overflow-x: auto;
          white-space: pre-wrap; }
button { font: inherit; padding: .5rem 1.2rem; cursor: pointer; }
button.spend { background: #b02a2a; color: #fff; border: none; }
nav a { margin-right: 1.2rem; }
details.evidence > summary { cursor: pointer; font-size: 1.3rem; font-weight: 700;
          margin: 1.5rem 0; }
.status-done { color: #1d7a1d; } .status-failed { color: #b02a2a; }
.status-running { color: #b06a00; } .status-queued, .status-proposed { color: #555; }
blockquote { border-left: 3px solid #ccc; margin-left: 0; padding-left: 1rem;
          color: #444; }
"""

#: The purpose statement the task requires on the form, verbatim visible text.
DISCLAIMER = (
    "This tool profiles how someone thinks from their public writing. "
    "It is not for candidate screening or employment decisions."
)


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_e(title)}</title><style>{_CSS}</style></head><body>"
        "<nav><a href='/'>New run</a><a href='/runs'>History</a>"
        "<a href='/audit'>Audit log</a></nav>"
        f"{body}</body></html>"
    )


def _when(ts: Any) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def _status(value: str) -> str:
    return f"<span class='status-{_e(value)}'>{_e(value)}</span>"


# -- the new-run form -------------------------------------------------------


def new_run_form(profiles: dict[str, str], error: str = "") -> str:
    options = "".join(
        f"<option value='{_e(key)}'>{_e(key)} — {_e(display)}</option>"
        for key, display in sorted(profiles.items())
    )
    error_block = f"<div class='error'>{_e(error)}</div>" if error else ""
    saved = (
        "<label for='target'>Saved profile (re-run in one click; leaves the fields below unused)</label>"
        f"<select name='target' id='target'><option value=''>— new target —</option>{options}</select>"
        if profiles
        else ""
    )
    return page(
        "corpus — new run",
        f"""
<h1>New run</h1>
<div class='notice'>{_e(DISCLAIMER)}</div>
{error_block}
<form class='run' method='post' action='/runs'>
  {saved}
  <div class='row'>
    <div><label for='name'>Name (required for a new target)</label>
         <input name='name' id='name' placeholder='Jane Smith'></div>
    <div><label for='key'>Key (required for a new target)</label>
         <input name='key' id='key' placeholder='janesmith'></div>
    <div><label for='x'>X handle</label><input name='x' id='x'></div>
    <div><label for='github'>GitHub username</label><input name='github' id='github'></div>
    <div><label for='site'>Site URL</label><input name='site' id='site'></div>
    <div><label for='employer'>Employer</label><input name='employer' id='employer'></div>
    <div><label for='role'>Role</label><input name='role' id='role'></div>
    <div><label for='budget'>Budget, dollars (hard stop)</label>
         <input name='budget' id='budget' value='10.00'></div>
    <div><label for='highlights'>Highlights cap (reduce input; the big cost lever)</label>
         <input name='highlights' id='highlights' value='60'></div>
  </div>
  <label for='reason'>Reason for this run (required — recorded in the audit log)</label>
  <textarea name='reason' id='reason' rows='2' required></textarea>
  <p><button type='submit'>Estimate first (dry run — nothing is spent)</button></p>
</form>
<p>Submitting runs the CLI's own <code>--dry-run</code> path. The paid run
only starts after you confirm on the estimate screen.</p>
""",
    )


# -- the estimate / confirm screen ------------------------------------------


def proposal_page(run: sqlite3.Row, token: str) -> str:
    return page(
        f"corpus — estimate for {run['target_key']}",
        f"""
<h1>Estimate for {_e(run["target_key"])}</h1>
<p>This is the CLI's own dry run: the plan and cost estimate below are what a
terminal user would see. <strong>Nothing has been spent.</strong></p>
<pre class='log'>{_e(run["dry_run_output"])}</pre>
<form method='post' action='/runs/{int(run["id"])}/confirm'>
  <input type='hidden' name='token' value='{_e(token)}'>
  <button type='submit' class='spend'>Start the paid run</button>
  <a href='/' style='margin-left:1rem'>Cancel</a>
</form>
<p>The budget hard stop shown in the plan applies unchanged once the run
starts — a web run is subject to exactly the same pre-flight as a terminal
run.</p>
""",
    )


# -- the run page ------------------------------------------------------------


def run_page(run: sqlite3.Row, lines: list[sqlite3.Row]) -> str:
    run_id = int(run["id"])
    status = str(run["status"])
    log_text = "\n".join(str(line["line"]) for line in lines)
    last_seq = int(lines[-1]["seq"]) if lines else 0
    error_block = (
        f"<div class='error'><strong>Failed:</strong> {_e(run['error'])}</div>"
        if run["error"]
        else ""
    )
    links = ""
    if status == "done" and run["out_dir"]:
        links = (
            f"<p><a href='/runs/{run_id}/report'>Open the report</a> · "
            f"<a href='/runs/{run_id}/files/report.md'>report.md</a> · "
            f"<a href='/runs/{run_id}/files/synthesis.json'>synthesis.json</a> · "
            f"<a href='/runs/{run_id}/files/corpus.json'>corpus.json</a> · "
            f"<a href='/runs/{run_id}/files/unconfirmed.md'>unconfirmed.md</a></p>"
        )
    poll = ""
    if status in ("queued", "running"):
        poll = f"""
<script>
let last = {last_seq};
async function poll() {{
  const resp = await fetch('/runs/{run_id}/log?after=' + last);
  const data = await resp.json();
  const log = document.getElementById('log');
  for (const entry of data.lines) {{
    log.textContent += (log.textContent ? '\\n' : '') + entry.line;
    last = entry.seq;
  }}
  if (data.status === 'queued' || data.status === 'running') {{
    setTimeout(poll, 2000);
  }} else {{
    location.reload();
  }}
}}
setTimeout(poll, 2000);
</script>"""
    return page(
        f"corpus — run {run_id}",
        f"""
<h1>Run {run_id}: {_e(run["target_key"])} — {_status(status)}</h1>
<p>Queued {_when(run["created_at"])}
{" · started " + _when(run["started_at"]) if run["started_at"] else ""}
{" · finished " + _when(run["finished_at"]) if run["finished_at"] else ""}</p>
{error_block}
{links}
<pre class='log' id='log'>{_e(log_text)}</pre>
{poll}
""",
    )


# -- history and audit --------------------------------------------------------


def _history_row(r: sqlite3.Row) -> str:
    run_id = int(r["id"])
    spend = f"${float(r['spend']):.4f}" if r["spend"] else ""
    docs = str(int(r["documents"])) if r["documents"] else ""
    report = (
        f"<a href='/runs/{run_id}/report'>report</a>"
        if str(r["status"]) == "done" and r["out_dir"]
        else ""
    )
    return (
        f"<tr><td><a href='/runs/{run_id}'>{run_id}</a></td>"
        f"<td>{_e(r['target_key'])}</td><td>{_when(r['created_at'])}</td>"
        f"<td>{_e(r['tier'])}</td><td>{docs}</td><td>{spend}</td>"
        f"<td>{_status(str(r['status']))}</td><td>{report}</td></tr>"
    )


def history_page(runs: list[sqlite3.Row]) -> str:
    rows = "".join(_history_row(r) for r in runs)
    return page(
        "corpus — history",
        "<h1>Run history</h1><table><tr><th>#</th><th>Target</th><th>Date</th>"
        "<th>Tier</th><th>Docs</th><th>Spend</th><th>Status</th><th></th></tr>"
        f"{rows}</table>",
    )


def audit_page(rows: list[sqlite3.Row]) -> str:
    body = "".join(
        f"<tr><td>{_when(r['ts'])}</td><td>{_e(r['who'])}</td>"
        f"<td>{_e(r['target_key'])}</td><td>${float(r['spend'] or 0):.4f}</td>"
        f"<td>{int(r['documents'] or 0) or ''}</td><td>{_e(r['tier'])}</td>"
        f"<td>{_status(str(r['status']))}</td><td>{_e(r['reason'])}</td></tr>"
        for r in rows
    )
    return page(
        "corpus — audit log",
        "<h1>Audit log</h1>"
        "<p>One row per run, whatever happened to it. Nothing deletes from this table.</p>"
        "<table><tr><th>When</th><th>Who</th><th>Target</th><th>Spend</th>"
        f"<th>Docs</th><th>Tier</th><th>Status</th><th>Reason</th></tr>{body}</table>",
    )


# -- the report ---------------------------------------------------------------


def report_page(run: sqlite3.Row, part_one_html: str, part_two_html: str) -> str:
    run_id = int(run["id"])
    return page(
        f"corpus — report for {run['target_key']}",
        f"""
{part_one_html}
<details class='evidence' id='evidence'><summary>The evidence</summary>
{part_two_html}</details>
<hr>
<p>Downloads: <a href='/runs/{run_id}/files/report.md'>report.md</a> ·
<a href='/runs/{run_id}/files/synthesis.json'>synthesis.json</a> ·
<a href='/runs/{run_id}/files/corpus.json'>corpus.json</a> ·
<a href='/runs/{run_id}/files/unconfirmed.md'>unconfirmed.md</a></p>
<script>
// Part one links into part two by section anchor; opening a link into a
// collapsed <details> must open it, or the link silently goes nowhere.
for (const link of document.querySelectorAll("a[href^='#']")) {{
  link.addEventListener('click', () => {{
    document.getElementById('evidence').open = true;
  }});
}}
</script>
""",
    )
