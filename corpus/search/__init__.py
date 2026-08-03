"""Phase 2 discovery: find sources the user does not already know about.

Phase 1 follows links out from the anchors and is free. This phase searches,
which costs money and — far more importantly — returns other people. The whole
job is: **search broadly, then prove each candidate belongs to the target
before it enters the corpus.**

Where coverage and attribution conflict, attribution wins. Every time.

One file per part of that, and the layering is the design:

* `providers.py`   — the vendor seam. A query string in, results out.
* `client.py`      — caching and billing around a provider.
* `queries.py`     — what to search for, built from the identity card.
* `pagefacts.py`   — what a fetched page says about who wrote it.
* `scoring.py`     — deterministic, offline, free: is this theirs?
* `verify.py`      — the two passes, discover then verify, and the phase itself.
* `unconfirmed.py` — the human-in-the-loop path for everything held back.

`scoring.py` is the file that decides whether this tool is trustworthy, and it
is deliberately the one with no network, no model, and no clock in it.
"""
