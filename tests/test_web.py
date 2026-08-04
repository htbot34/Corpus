"""The web layer: a wrapper around the CLI, and the guarantees that keep it one.

Five properties, each from a real failure mode this project has already paid
for elsewhere: the queue is serial and survives a dead job; the audit row
survives a failed run; the dry-run gate cannot be bypassed by posting at the
run endpoint; no endpoint ever returns an API key; and the server refuses to
bind anywhere the network can see.

The pipeline itself is faked throughout — these tests are about the wrapper,
and the wrapper's contract is argv in, streamed lines out.
"""

from __future__ import annotations

import threading
import time
from itertools import pairwise
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from corpus.cli import app as cli_app
from corpus.web.app import DOWNLOADABLE, create_app, ensure_loopback
from corpus.web.markdown import markdown_to_html
from corpus.web.runner import FormError, RunOutcome, RunParams, profile_argv, run_argv
from corpus.web.store import WebStore
from corpus.web.worker import RunWorker

runner = CliRunner()


@pytest.fixture()
def store(tmp_path: Path) -> WebStore:
    s = WebStore(path=tmp_path / "web.db")
    yield s
    s.close()


def form_fields(**overrides: str) -> dict[str, str]:
    base = {
        "name": "Jane Smith",
        "key": "janesmith",
        "budget": "5.00",
        "highlights": "12",
        "reason": "candidate for a research collaboration",
    }
    base.update(overrides)
    return base


def ok_dry_run(_params: RunParams) -> tuple[int, str]:
    return 0, "Estimated cost\n    total: ~$0.42 of $5.00 budget\n--dry-run: stopping"


def make_app(store: WebStore, *, execute=None, dry_runner=ok_dry_run) -> TestClient:
    fake = execute if execute is not None else (lambda p, sink: RunOutcome(exit_code=0))
    return TestClient(create_app(store, execute=fake, dry_runner=dry_runner, start_worker=False))


def propose(client: TestClient, **overrides: str) -> int:
    resp = client.post("/runs", data=form_fields(**overrides), follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return int(resp.headers["location"].rsplit("/", 1)[-1])


# -- the queue: serial, and it survives its jobs ---------------------------


def test_the_queue_runs_one_job_at_a_time_and_survives_a_failing_job(store: WebStore) -> None:
    intervals: list[tuple[str, float, float]] = []
    lock = threading.Lock()

    def fake_execute(params: RunParams, sink) -> RunOutcome:
        start = time.monotonic()
        sink(f"working on {params.key}")
        time.sleep(0.03)
        if params.key == "bad":
            with lock:
                intervals.append((params.key, start, time.monotonic()))
            raise RuntimeError("the provider died mid-run")
        with lock:
            intervals.append((params.key, start, time.monotonic()))
        return RunOutcome(exit_code=0, spend=0.25, documents=7, tier="thin")

    ids = []
    for key in ("good1", "bad", "good2"):
        run_id, token = store.propose(
            target_key=key,
            name=key,
            params=RunParams(key=key, name=key).as_dict(),
            reason="test",
            dry_run_output="estimate",
        )
        assert store.confirm(run_id, token)
        ids.append(run_id)

    worker = RunWorker(store, execute=fake_execute, poll_seconds=0.01)
    worker.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        rows = {int(r["id"]): str(r["status"]) for r in store.runs()}
        if all(rows[i] in ("done", "failed") for i in ids):
            break
        time.sleep(0.02)
    worker.stop()

    statuses = {int(r["id"]): str(r["status"]) for r in store.runs()}
    assert statuses[ids[0]] == "done"
    assert statuses[ids[1]] == "failed", "a raised exception must mark the run failed"
    assert statuses[ids[2]] == "done", "the queue must survive the dead job and keep going"

    # Serial means serial: no execution interval overlaps another.
    ordered = sorted(intervals, key=lambda entry: entry[1])
    for (_, _, earlier_end), (_, later_start, _) in pairwise(ordered):
        assert earlier_end <= later_start, "two runs executed concurrently"

    # The failure surfaced in full, not summarized.
    failed = store.get_run(ids[1])
    assert failed is not None and "the provider died mid-run" in str(failed["error"])
    log_text = "\n".join(str(entry["line"]) for entry in store.log_lines(ids[1]))
    assert "WORKER ERROR" in log_text and "the provider died mid-run" in log_text


def test_the_audit_row_is_written_even_when_the_run_fails(store: WebStore) -> None:
    def fake_execute(params: RunParams, sink) -> RunOutcome:
        raise RuntimeError("boom")

    run_id, token = store.propose(
        target_key="jane",
        name="Jane",
        params=RunParams(key="jane", name="Jane").as_dict(),
        reason="due diligence on a co-founder",
        dry_run_output="estimate",
    )
    assert store.confirm(run_id, token)
    worker = RunWorker(store, execute=fake_execute)
    assert worker.process_next() is True

    rows = store.audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert str(row["status"]) == "failed"
    assert str(row["target_key"]) == "jane"
    assert str(row["reason"]) == "due diligence on a co-founder"
    assert str(row["who"]) == "local"


def test_the_store_offers_no_way_to_delete_audit_rows() -> None:
    """Append-only by construction: the class has no destructive method at all."""
    destructive = [
        n
        for n in dir(WebStore)
        if any(word in n.lower() for word in ("delete", "remove", "drop", "clear", "purge"))
    ]
    assert destructive == []


# -- the dry-run gate -------------------------------------------------------


def test_the_dry_run_gate_cannot_be_bypassed(store: WebStore) -> None:
    paid_calls: list[str] = []

    def paid(params: RunParams, sink) -> RunOutcome:
        paid_calls.append(params.key)
        return RunOutcome(exit_code=0)

    with make_app(store, execute=paid) as client:
        # Posting straight at the confirm endpoint, with no proposal: refused.
        assert client.post("/runs/999/confirm", data={"token": "forged"}).status_code == 409

        # The form endpoint can only ever create a proposal — never a queued run.
        run_id = propose(client)
        row = store.get_run(run_id)
        assert row is not None and str(row["status"]) == "proposed"

        # A wrong token is refused; so is a missing one.
        assert client.post(f"/runs/{run_id}/confirm", data={"token": "wrong"}).status_code == 409
        assert client.post(f"/runs/{run_id}/confirm", data={}).status_code == 409
        row = store.get_run(run_id)
        assert row is not None and str(row["status"]) == "proposed"

        # The real token queues it — once. A replay is refused.
        token = str(row["token"])
        assert (
            client.post(
                f"/runs/{run_id}/confirm", data={"token": token}, follow_redirects=False
            ).status_code
            == 303
        )
        row = store.get_run(run_id)
        assert row is not None and str(row["status"]) == "queued"
        assert client.post(f"/runs/{run_id}/confirm", data={"token": token}).status_code == 409

        # Nothing in any of the above touched the paid executor.
        assert paid_calls == []


def test_a_failed_dry_run_never_becomes_a_proposal(store: WebStore) -> None:
    def broken_dry_run(_params: RunParams) -> tuple[int, str]:
        return 2, "ERROR: X_API_KEY is not set. Get a key at https://twitterapi.io"

    with make_app(store, dry_runner=broken_dry_run) as client:
        resp = client.post("/runs", data=form_fields(), follow_redirects=False)
        assert resp.status_code == 502
        # The CLI's own words, in full, not a summary.
        assert "X_API_KEY is not set" in resp.text
        assert store.runs() == []


def test_the_reason_field_is_required(store: WebStore) -> None:
    with make_app(store) as client:
        resp = client.post("/runs", data=form_fields(reason=""), follow_redirects=False)
        assert resp.status_code == 422
        assert "reason" in resp.text
        assert store.runs() == []


def test_the_form_requires_name_and_key(store: WebStore) -> None:
    with make_app(store) as client:
        resp = client.post("/runs", data=form_fields(name="", key=""))
        assert resp.status_code == 422
        assert "name and key are required" in resp.text


# -- no key ever reaches the browser ---------------------------------------


def test_no_endpoint_returns_an_api_key(store: WebStore, monkeypatch: pytest.MonkeyPatch) -> None:
    sentinels = {
        "ANTHROPIC_API_KEY": "SENTINEL-ANTHROPIC-VALUE-A9243",
        "X_API_KEY": "SENTINEL-X-VALUE-B7710",
        "GITHUB_TOKEN": "SENTINEL-GITHUB-VALUE-C3358",
    }
    for name, value in sentinels.items():
        monkeypatch.setenv(name, value)

    def fake_execute(params: RunParams, sink) -> RunOutcome:
        sink("map slice 1/2: 3 topics ($0.01)")
        return RunOutcome(exit_code=0, spend=0.5, documents=10, tier="thin")

    with make_app(store, execute=fake_execute) as client:
        run_id = propose(client)
        row = store.get_run(run_id)
        assert row is not None
        client.post(f"/runs/{run_id}/confirm", data={"token": str(row["token"])})
        client.app.state.worker.process_next()  # type: ignore[union-attr]

        paths = [
            "/",
            "/runs",
            f"/runs/{run_id}",
            f"/runs/{run_id}/log",
            "/audit",
            f"/runs/{run_id}/report",
        ]
        for path in paths:
            body = client.get(path).text
            for name, value in sentinels.items():
                assert value not in body, f"{name} leaked into GET {path}"


# -- the wrapper wraps; it does not reimplement -----------------------------


def test_run_argv_is_the_same_command_a_terminal_user_would_type() -> None:
    params = RunParams(key="janesmith", name="Jane Smith", budget=5.0, highlights=12)
    dry = run_argv(params, dry_run=True)
    paid = run_argv(params, dry_run=False)

    assert "corpus.cli" in " ".join(dry)
    for argv in (dry, paid):
        assert argv[argv.index("--target") + 1] == "janesmith"
        assert argv[argv.index("--budget") + 1] == "5.00"
        assert argv[argv.index("--highlights") + 1] == "12"
    assert dry[-1] == "--dry-run", "the preview path must be the CLI's own dry run"
    assert paid[-1] == "--yes", "the confirm screen replaces the terminal prompt"
    assert "--dry-run" not in paid


def test_profile_argv_carries_only_the_fields_the_form_filled() -> None:
    params = RunParams(key="jane", name="Jane Smith", github="jsmith")
    argv = profile_argv(params)
    assert argv[argv.index("--key") + 1] == "jane"
    assert argv[argv.index("--github") + 1] == "jsmith"
    assert "--x" not in argv and "--site" not in argv and "--employer" not in argv


def test_form_parsing_fails_loudly_on_a_bad_budget() -> None:
    with pytest.raises(FormError):
        RunParams.from_form(form_fields(budget="lots"))
    with pytest.raises(FormError):
        RunParams.from_form(form_fields(budget="-3"))


def test_selecting_a_saved_profile_skips_the_profile_save() -> None:
    params = RunParams.from_form({"target": "janesmith", "reason": "x", "budget": "2"})
    assert params.key == "janesmith"
    assert params.save_profile is False


# -- report rendering -------------------------------------------------------

TWO_PART_REPORT = """# Jane Smith — how they think

## Where they land

**epistemics** — strong signal, 3 cited document(s). They demand falsifiable claims.

[Full reasoning chains](#the-axes-in-full)

---

# The evidence

> **Coverage and caveats**
> - Date range: 2020..2025

## The axes in full

Evidence: [2025-03-01](https://blog.example.com/post)
"""


def seed_finished_run(store: WebStore, tmp_path: Path) -> int:
    out_dir = tmp_path / "out" / "janesmith" / "2026-08-04"
    out_dir.mkdir(parents=True)
    (out_dir / "report.md").write_text(TWO_PART_REPORT, encoding="utf-8")
    (out_dir / "synthesis.json").write_text("{}", encoding="utf-8")
    run_id, token = store.propose(
        target_key="janesmith",
        name="Jane Smith",
        params=RunParams(key="janesmith", name="Jane Smith").as_dict(),
        reason="test",
        dry_run_output="estimate",
    )
    store.confirm(run_id, token)
    store.finish(
        run_id, status="done", out_dir=str(out_dir), spend=1.5, documents=20, tier="moderate"
    )
    return run_id


def test_the_report_page_opens_on_part_one_with_the_evidence_a_click_away(
    store: WebStore, tmp_path: Path
) -> None:
    run_id = seed_finished_run(store, tmp_path)
    with make_app(store) as client:
        body = client.get(f"/runs/{run_id}/report").text

    assert "Where they land" in body
    # Part two is present but collapsed: a <details> element, not a second page.
    assert "<details" in body and "The evidence" in body
    # Evidence links go to the source URLs, so a claim can be checked in place.
    assert 'href="https://blog.example.com/post"' in body
    # Part one's section links survive as anchors into part two.
    assert 'href="#the-axes-in-full"' in body and 'id="the-axes-in-full"' in body


def test_downloads_serve_only_the_whitelisted_files(store: WebStore, tmp_path: Path) -> None:
    run_id = seed_finished_run(store, tmp_path)
    with make_app(store) as client:
        assert client.get(f"/runs/{run_id}/files/report.md").status_code == 200
        assert client.get(f"/runs/{run_id}/files/synthesis.json").status_code == 200
        # Not on the whitelist: refused, whatever exists on disk.
        assert client.get(f"/runs/{run_id}/files/run.json").status_code == 404
        assert client.get(f"/runs/{run_id}/files/..%2F..%2Fsecrets.txt").status_code == 404
    assert set(DOWNLOADABLE) == {"report.md", "synthesis.json", "corpus.json", "unconfirmed.md"}


def test_markdown_covers_the_reports_vocabulary() -> None:
    html = markdown_to_html(
        "## The axes in full\n\n**bold** and _italic_ and `code`\n\n"
        "- item one\n- item two\n\n> caveat line\n\n---\n\n"
        "[link](https://a.example/x)\n\n```\nspend table\n```\n"
    )
    assert '<h2 id="the-axes-in-full">' in html
    assert "<strong>bold</strong>" in html and "<em>italic</em>" in html
    assert "<code>code</code>" in html
    assert "<li>item one</li>" in html
    assert "<blockquote>" in html and "<hr>" in html
    assert '<a href="https://a.example/x">link</a>' in html
    assert "<pre><code>spend table" in html


def test_markdown_escapes_html_in_document_text() -> None:
    html = markdown_to_html("A title containing <script>alert(1)</script> tags")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# -- loopback only ----------------------------------------------------------


def test_the_server_refuses_to_bind_to_a_non_loopback_address() -> None:
    for host in ("0.0.0.0", "192.168.1.10", "10.0.0.5", "example.com", ""):
        with pytest.raises(ValueError, match="refusing to bind"):
            ensure_loopback(host)
    for host in ("127.0.0.1", "localhost", "::1"):
        ensure_loopback(host)  # must not raise


def test_the_serve_command_refuses_a_public_bind_before_starting_anything() -> None:
    result = runner.invoke(cli_app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "refusing to bind" in result.output
