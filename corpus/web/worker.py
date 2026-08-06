"""One background thread, one run at a time.

Runs take minutes and cost money, so the queue is deliberately serial: a
second confirmed run waits until the first finishes, however it finishes. A
failing run — a raised exception, a non-zero exit, a dead provider — is
recorded in full and the worker moves on to the next job; the queue never
dies with its patient.

The audit row is written in the same place for every terminal state, which
is what makes "the audit row is written even when a run fails" a property
of the control flow rather than a hope.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from .runner import RunOutcome, RunParams, execute_run
from .store import WebStore

Executor = Callable[[RunParams, Callable[[str], None]], RunOutcome]


class RunWorker:
    def __init__(
        self,
        store: WebStore,
        *,
        execute: Executor = execute_run,
        who: str = "local",
        poll_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self.execute = execute
        self.who = who
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="corpus-web-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.process_next():
                self._stop.wait(self.poll_seconds)

    # -- one job -----------------------------------------------------------

    def process_next(self) -> bool:
        """Claim and run the oldest queued job. Returns False on an empty queue."""
        row = self.store.claim_next()
        if row is None:
            return False
        run_id = int(row["id"])
        params = RunParams.from_json(str(row["params"]))

        outcome = RunOutcome(exit_code=-1)
        error = ""
        try:
            outcome = self.execute(params, lambda line: self.store.append_log(run_id, line))
            if outcome.exit_code != 0:
                error = f"corpus run exited with code {outcome.exit_code}"
        except Exception as exc:  # a dead job must not kill the queue
            error = f"{type(exc).__name__}: {exc}"
            # In full, where the run page shows it — never summarized.
            self.store.append_log(run_id, f"WORKER ERROR: {error}")
        for note in outcome.notes:
            self.store.append_log(run_id, note)

        # Three terminal states, not two: a run that completed cleanly with an
        # empty corpus is "the tool worked and the answer is no", and painting
        # it with the error treatment would get it re-run repeatedly at cost.
        if error:
            status = "failed"
        elif outcome.empty_corpus:
            status = "no_corpus"
        else:
            status = "done"
        self.store.finish(
            run_id,
            status=status,
            error=error,
            out_dir=outcome.out_dir,
            spend=outcome.spend,
            documents=outcome.documents,
            tier=outcome.tier,
        )
        # Every terminal state leaves an audit row: success, non-zero exit,
        # and raised exception all pass through this line.
        self.store.append_audit(
            who=self.who,
            target_key=str(row["target_key"]),
            reason=str(row["reason"]),
            spend=outcome.spend,
            documents=outcome.documents,
            tier=outcome.tier,
            status=status,
            run_id=run_id,
        )
        return True
