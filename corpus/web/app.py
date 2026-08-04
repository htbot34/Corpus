"""Routes. Thin by design: parse, delegate to the store/runner, render.

The two properties the routes themselves enforce:

* **The dry-run gate.** POST /runs only ever creates a *proposal* — it runs
  the CLI's own --dry-run and shows the estimate. The single path to a paid
  run is POST /runs/{id}/confirm with the proposal's token, so posting
  directly at any endpoint cannot start spending.
* **Loopback only.** `ensure_loopback` refuses anything but 127.0.0.1 /
  localhost / ::1. This runs on one laptop; auth and hosting come later and
  nothing here pretends otherwise.

No environment variable is ever read into a page, a URL, or the store: the
subprocess inherits the keys, and that is the only place they go.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from . import pages
from .runner import FormError, RunParams, execute_dry_run
from .store import WebStore
from .worker import Executor, RunWorker

DryRunner = Callable[[RunParams], tuple[int, str]]

#: The only files a run directory serves, by exact name. Everything else in
#: the directory — and everything outside it — is unreachable.
DOWNLOADABLE = {
    "report.md": "text/markdown",
    "synthesis.json": "application/json",
    "corpus.json": "application/json",
    "unconfirmed.md": "text/markdown",
}


def ensure_loopback(host: str) -> None:
    """Refuse to bind anywhere the network can see.

    This is a single-laptop tool with no authentication, and the way to keep
    that honest is to make the unsafe configuration unrepresentable rather
    than discouraged.
    """
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"refusing to bind to {host!r}: the web interface is local-only and "
            "serves without authentication. Only 127.0.0.1, localhost, or ::1 "
            "are accepted; remote hosting comes later, with auth, or not at all."
        )


def _profiles() -> dict[str, str]:
    """Saved targets for the form's dropdown, tolerant of an absent file."""
    from ..identity import load_targets

    try:
        return {key: card.display for key, card in load_targets().items()}
    except Exception:
        return {}


def create_app(
    store: WebStore | None = None,
    *,
    execute: Executor | None = None,
    dry_runner: DryRunner | None = None,
    who: str = "local",
    start_worker: bool = True,
) -> FastAPI:
    """Build the app. Tests inject a store, an executor, and a dry-runner."""
    web_store = store if store is not None else WebStore()
    run_dry = dry_runner if dry_runner is not None else execute_dry_run
    worker = (
        RunWorker(web_store, execute=execute, who=who)
        if execute is not None
        else RunWorker(web_store, who=who)
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            worker.stop()

    app = FastAPI(lifespan=lifespan)
    app.state.store = web_store
    app.state.worker = worker

    def _run_or_404(run_id: int) -> tuple[sqlite3.Row | None, HTMLResponse | None]:
        row = web_store.get_run(run_id)
        if row is None:
            return None, HTMLResponse(
                pages.page("not found", f"<h1>No run {run_id}</h1>"), status_code=404
            )
        return row, None

    # -- form and gate -----------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(pages.new_run_form(_profiles()))

    @app.post("/runs")
    async def create_proposal(request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        reason = (form.get("reason") or "").strip()
        try:
            if not reason:
                raise FormError("a reason for the run is required; it is recorded in the audit log")
            params = RunParams.from_form(form)
        except FormError as exc:
            return HTMLResponse(pages.new_run_form(_profiles(), error=str(exc)), status_code=422)

        # The CLI's own --dry-run, synchronously: free, and the output below
        # is the estimate screen. A non-zero exit is shown in full — a dry
        # run that failed is not a proposal anyone should confirm.
        code, output = run_dry(params)
        if code != 0:
            body = (
                "<h1>Dry run failed</h1><p>The paid run was not proposed. "
                "The CLI's full output:</p>"
                f"<pre class='log'>{pages._e(output)}</pre><p><a href='/'>Back</a></p>"
            )
            return HTMLResponse(pages.page("corpus — dry run failed", body), status_code=502)

        run_id, _token = web_store.propose(
            target_key=params.key,
            name=params.name,
            params=params.as_dict(),
            reason=reason,
            dry_run_output=output,
        )
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/confirm")
    async def confirm(run_id: int, request: Request) -> Response:
        form = await request.form()
        token = str(form.get("token") or "")
        if not web_store.confirm(run_id, token):
            # Wrong token, unknown run, or a run that never went through the
            # dry-run gate: refused, and the refusal says why.
            return HTMLResponse(
                pages.page(
                    "corpus — refused",
                    "<h1>Confirmation refused</h1><p>Paid runs start only from a "
                    "proposal created by the dry-run screen, using its token. "
                    "This request matched no such proposal.</p><p><a href='/'>Back</a></p>",
                ),
                status_code=409,
            )
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    # -- run pages ---------------------------------------------------------

    @app.get("/runs", response_class=HTMLResponse)
    async def history() -> HTMLResponse:
        return HTMLResponse(pages.history_page(web_store.runs()))

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_view(run_id: int) -> HTMLResponse:
        row, missing = _run_or_404(run_id)
        if missing is not None:
            return missing
        assert row is not None
        if str(row["status"]) == "proposed":
            return HTMLResponse(pages.proposal_page(row, str(row["token"])))
        return HTMLResponse(pages.run_page(row, web_store.log_lines(run_id)))

    @app.get("/runs/{run_id}/log")
    async def run_log(run_id: int, after: int = 0) -> JSONResponse:
        row, missing = _run_or_404(run_id)
        if missing is not None:
            return JSONResponse({"error": f"no run {run_id}"}, status_code=404)
        assert row is not None
        lines = [
            {"seq": int(entry["seq"]), "line": str(entry["line"])}
            for entry in web_store.log_lines(run_id, after=after)
        ]
        return JSONResponse({"status": str(row["status"]), "lines": lines})

    @app.get("/audit", response_class=HTMLResponse)
    async def audit() -> HTMLResponse:
        return HTMLResponse(pages.audit_page(web_store.audit_rows()))

    # -- report and downloads ----------------------------------------------

    @app.get("/runs/{run_id}/report", response_class=HTMLResponse)
    async def report(run_id: int) -> HTMLResponse:
        from .markdown import markdown_to_html

        row, missing = _run_or_404(run_id)
        if missing is not None:
            return missing
        assert row is not None
        out_dir = str(row["out_dir"])
        report_path = Path(out_dir) / "report.md" if out_dir else None
        if report_path is None or not report_path.exists():
            return HTMLResponse(
                pages.page(
                    "corpus — no report",
                    f"<h1>Run {run_id} has no report</h1><p>Status: "
                    f"{pages._e(str(row['status']))}. "
                    "The report appears when a run finishes.</p>",
                ),
                status_code=404,
            )
        text = report_path.read_text(encoding="utf-8")
        # The report's own structure: part one, then "# The evidence".
        head, marker, tail = text.partition("\n# The evidence")
        if marker:
            part_one = markdown_to_html(head)
            part_two = markdown_to_html(tail)
        else:  # a pre-split report.md, rendered whole rather than hidden
            part_one = markdown_to_html(text)
            part_two = "<p><em>This report predates the two-part layout.</em></p>"
        return HTMLResponse(pages.report_page(row, part_one, part_two))

    @app.get("/runs/{run_id}/files/{filename}")
    async def download(run_id: int, filename: str) -> Response:
        row, missing = _run_or_404(run_id)
        if missing is not None:
            return missing
        assert row is not None
        media_type = DOWNLOADABLE.get(filename)
        out_dir = str(row["out_dir"])
        if media_type is None or not out_dir:
            return HTMLResponse(
                pages.page("not found", f"<h1>No such file for run {run_id}</h1>"),
                status_code=404,
            )
        target = Path(out_dir) / filename
        if not target.exists():
            return HTMLResponse(
                pages.page(
                    "not found", f"<h1>{pages._e(filename)} does not exist for this run</h1>"
                ),
                status_code=404,
            )
        return FileResponse(target, media_type=media_type, filename=filename)

    return app


def queue_snapshot(store: WebStore) -> str:
    """One line for the CLI's serve banner: what is waiting."""
    rows = store.runs(limit=50)
    queued = sum(1 for r in rows if str(r["status"]) == "queued")
    running = sum(1 for r in rows if str(r["status"]) == "running")
    if not queued and not running:
        return "queue empty"
    return f"{running} running, {queued} queued"


__all__ = ["DOWNLOADABLE", "create_app", "ensure_loopback", "queue_snapshot"]
