"""The local web interface: a thin wrapper around the CLI, never a second pipeline.

Everything a run does here, it does by invoking the same `corpus run` a
terminal user would type — same functions, same budget pre-flight, same
`.env`, same output. The web layer adds exactly four things: a form, a
dry-run gate in front of the paid run, a one-at-a-time queue, and an audit
log. It binds to loopback only; auth and hosting are deliberately absent
rather than pretended at.
"""
