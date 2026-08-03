"""Turn the live GitHub captures into fixtures. Run once; kept as provenance.

PROVENANCE. Unlike the X fixtures next to these, the GitHub fixtures are
**real shapes**: they were derived from three payloads pulled from the live API
with an authenticated token on 2026-08-03 —

    GET /users/{login}
    GET /users/{login}/events/public?per_page=100
    GET /search/issues?q=commenter:{login}+type:pr

The captures lived in `gh_captures/` and were deleted after this script ran,
because they carry a real person's identifiers and 290KB of other people's
prose. This file is the record of exactly what was changed.

WHAT IS REAL, and is the entire point of the fixtures:

* every key name, at every level of nesting
* which keys are present and which are absent — including the finding that
  drove the adapter's design, that no PushEvent payload carries `commits`
* the mix of event types, and the array-versus-envelope shape of each endpoint
* timestamp formats, URL formats, and null-versus-missing

WHAT IS SCRUBBED, because none of it is load-bearing for a wire contract:

* logins, display names, numeric ids, node ids, avatars, gravatar ids
* comment, issue, and commit body text, replaced with synthetic prose that
  keeps the shape that matters (CRLF line endings, markdown, backtick code
  spans, sign-off trailers, the `@mention` form)

One fixture here is not derived from a capture at all and says so in its own
filename: `github_events_with_commits.json`. It exists because the live
PushEvents carry no `commits` array, so nothing in the real capture can
exercise the commit-message path. It is written to GitHub's *documented*
PushEvent shape and is labelled SYNTHETIC in the wire contract.

Run: python tests/fixtures/_scrub_github.py  (requires gh_captures/, deleted)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
CAPTURES = HERE.parents[1] / "gh_captures"

SUBJECT = "testsubject"
SUBJECT_ID = 4200001
OTHERS = ["contributor-one", "contributor-two", "contributor-three", "contributor-four"]

# Synthetic bodies. Each keeps a shape the adapter or a test depends on: CRLF,
# an indented block, a backtick span, an @mention, a sign-off trailer.
REVIEW_BODY = (
    "The validation looks right, but I'd keep a bitmap in an `unsigned int` of "
    "the effects already used, and initialise it to 1 since effect 0 is the "
    "implicit first one. Ok?"
)
ISSUE_BODIES = [
    "This looks correct, but the commit message is misformed and has no "
    "sign-offs.\r\n\r\n    Reported-by: @contributor-one\r\n\r\nI've fixed it "
    "differently in my tree and will push once the other changes are tested.",
    "Looks sane, merged",
    "This is one of the ones that got fixed in today's flurry of commits",
    "No. The whole point of the ordering is that it is stable across builds — "
    "if you sort by a value two files can share, you have not fixed anything, "
    "you have moved the coin flip somewhere else.",
    "I'd rather take the boring fix here. `os.listdir()` ordering is not "
    "something I want load-bearing.",
]
PR_TITLES = [
    "fix: give the phaser a unique effect priority",
    "parametric_eq: fix gain math",
    "prep: fix the Makefile to fetch all submodules",
    "Refactor the echo effect and simplify its controls",
    "Add a noise gate threshold control",
]
PR_BODIES = [
    "Two headers both declared the same priority. The generator assigns each "
    "effect an id from its index in a stable sort by priority, over files "
    "enumerated with `os.listdir()`. With a tie, the id depends on directory "
    "order.",
    "The biquad helpers take a gain argument in dB, but the pot only ever "
    "delivered about half the requested range.",
    "There is no recursive submodule init, so the checkout has to be done by "
    "hand before anything builds.",
    "The result is a more accurate tape echo model and a cleaner UI.\r\n\r\n"
    "    - channel gain is now per-tap\r\n    - the mix control is logarithmic",
    "Adds a threshold control so the gate can be tuned per instrument.",
]
DIFF_HUNK = (
    "@@ -339,13 +339,24 @@ static void handle_payload(uint8_t *buf, size_t len)\n"
    " \t} else if (cmd == 0x08) { // set routing order\n"
    " \t\trouted_effect_count = 0;\n"
    "+\t\tif (len < 2)\n"
    "+\t\t\treturn;\n"
)

# Kept verbatim in shape, rewritten in content. The three that must survive the
# low-signal filter are the ones with an argument in them.
COMMIT_MESSAGES = [
    "audio: keep the gate's threshold in fixed point\n\n"
    "Floating point in the interrupt path was costing more than the precision "
    "was worth, and the range we actually need fits in Q15 with room to spare.\n\n"
    "Signed-off-by: Test Subject <subject@example.invalid>",
    "wip",
    "fix typo",
    "Merge branch 'fixes' of https://github.com/contributor-one/project",
    "gen_effects: sort by (priority, filename) so ids are stable\n\n"
    "Two effects sharing a priority made the id depend on os.listdir() order, "
    "which is to say on the filesystem. Break the tie explicitly.",
    "WIP: do not merge",
    'Revert "audio: keep the gate\'s threshold in fixed point"\n\n'
    "This reverts commit 1111111111111111111111111111111111111111. The Q15 "
    "range is wrong for the high-gain path and it clips.",
]


# Real repository names are as identifying as a login, so they go too. The
# mapping is stable across the run so cross-references inside a payload still
# line up — a fixture whose repo.url disagrees with its repo.name would fail the
# contract for a reason that is an artifact of the scrub.
REPOS = ["project-alpha", "project-beta", "project-gamma"]

# Numeric ids are replaced by a deterministic remap rather than a constant:
# equal ids in the capture must stay equal in the fixture, and different ids
# must stay different, or the contract test cannot tell the two cases apart.
_ID_KEYS = {"id", "repository_id", "push_id", "pull_request_review_id", "installation_id"}


def _fake_id(real: Any, seen: dict[int, int]) -> Any:
    if not isinstance(real, int):
        return real
    if real not in seen:
        seen[real] = 4200000 + len(seen) + 1
    return seen[real]


def _swap_identity(
    node: Any,
    subject_login: str,
    others: dict[str, str],
    ids: dict[int, int],
    repos: dict[str, str],
) -> Any:
    """Replace logins, repo names, and ids, leaving every key name untouched."""
    if isinstance(node, list):
        return [_swap_identity(v, subject_login, others, ids, repos) for v in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("avatar_url", "gravatar_id"):
            out[key] = ""
        elif key == "node_id":
            out[key] = "SCRUBBED"
        elif key in _ID_KEYS and isinstance(value, int):
            out[key] = _fake_id(value, ids)
        elif key in ("login", "display_login") and isinstance(value, str):
            out[key] = (
                SUBJECT
                if value == subject_login
                else others.setdefault(value, OTHERS[len(others) % len(OTHERS)])
            )
        elif isinstance(value, str):
            replaced = value
            for real, fake in repos.items():
                replaced = replaced.replace(real, fake)
            replaced = replaced.replace(subject_login, SUBJECT)
            for real, fake in others.items():
                replaced = replaced.replace(real, fake)
            out[key] = replaced
        else:
            out[key] = _swap_identity(value, subject_login, others, ids, repos)
    return out


def _repo_map(raw: Any) -> dict[str, str]:
    """Every repository short name in the capture, mapped to a synthetic one.

    Built before any substitution so the longest names are replaced first;
    otherwise a repo whose name is a prefix of another corrupts it.
    """
    names: set[str] = set()
    # Repo names appear in `name`/`full_name` on some objects and only inside
    # URLs on others — a search item carries `repository_url` and no name at
    # all — so both are swept. The URL pattern requires a repo-shaped context:
    # without it, `api.github.com/users/{login}` matches too and the *owner*
    # gets rewritten as a repository.
    url_repo = re.compile(
        r"(?:api\.github\.com/repos/[\w.-]+/([\w.-]+)"
        r"|github\.com/[\w.-]+/([\w.-]+)/(?:pull|issues|commit|blob|tree))"
    )

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            full = node.get("name") or node.get("full_name")
            if isinstance(full, str) and full.count("/") == 1:
                names.add(full.split("/", 1)[1])
            for value in node.values():
                if isinstance(value, str):
                    for groups in url_repo.findall(value):
                        names.update(g for g in groups if g)
                walk(value)

    walk(raw)
    names.discard("")
    ordered = sorted(names, key=len, reverse=True)
    return {name: REPOS[index % len(REPOS)] for index, name in enumerate(ordered)}


def scrub_events(raw: list[dict[str, Any]], subject: str) -> list[dict[str, Any]]:
    """A representative subset: every observed type, comment bodies replaced."""
    others: dict[str, str] = {}
    ids: dict[int, int] = {}
    repos = _repo_map(raw)
    by_type: dict[str, list[dict[str, Any]]] = {}
    for event in raw:
        by_type.setdefault(event["type"], []).append(event)

    keep: list[dict[str, Any]] = []
    keep.extend(by_type.get("PushEvent", [])[:4])
    keep.extend(by_type.get("IssueCommentEvent", [])[:5])
    keep.extend(by_type.get("PullRequestReviewCommentEvent", [])[:2])
    keep.extend(by_type.get("PullRequestEvent", [])[:2])
    keep.extend(by_type.get("IssuesEvent", [])[:1])
    keep.sort(key=lambda e: e["created_at"], reverse=True)

    scrubbed = _swap_identity(keep, subject, others, ids, repos)
    issue_body = iter(ISSUE_BODIES * 4)
    for event in scrubbed:
        payload = event["payload"]
        if event["type"] == "IssueCommentEvent":
            payload["comment"]["body"] = next(issue_body)
            payload["issue"]["body"] = "Synthetic issue body, shape preserved."
            payload["issue"]["title"] = PR_TITLES[hash(event["id"]) % len(PR_TITLES)]
        elif event["type"] == "PullRequestReviewCommentEvent":
            payload["comment"]["body"] = REVIEW_BODY
            payload["comment"]["diff_hunk"] = DIFF_HUNK
        elif event["type"] == "IssuesEvent":
            payload["issue"]["body"] = "Synthetic issue body, shape preserved."
            payload["issue"]["title"] = PR_TITLES[0]
    return scrubbed


def scrub_search(raw: dict[str, Any], subject: str) -> dict[str, Any]:
    others: dict[str, str] = {}
    ids: dict[int, int] = {}
    repos = _repo_map(raw)
    trimmed = dict(raw)
    trimmed["items"] = _swap_identity(raw["items"][:5], subject, others, ids, repos)
    for index, item in enumerate(trimmed["items"]):
        item["title"] = PR_TITLES[index % len(PR_TITLES)]
        item["body"] = PR_BODIES[index % len(PR_BODIES)]
    return trimmed


def synthetic_push_with_commits(template: dict[str, Any]) -> list[dict[str, Any]]:
    """NOT a capture. The documented PushEvent shape, so the commit path has
    something to run against — see this module's docstring."""
    event = json.loads(json.dumps(template))
    event["id"] = "99000000001"
    event["payload"] = {
        "repository_id": event["payload"]["repository_id"],
        "push_id": 99000000001,
        "size": len(COMMIT_MESSAGES),
        "distinct_size": len(COMMIT_MESSAGES),
        "ref": "refs/heads/main",
        "head": "a" * 40,
        "before": "b" * 40,
        # Shas differ in their leading characters, as real ones do. An earlier
        # draft zero-padded an index, which made every abbreviated sha
        # identical and caught a real bug in the adapter's document id.
        "commits": [
            {
                "sha": f"{index + 1:x}" * 40,
                "author": {"email": "subject@example.invalid", "name": "Test Subject"},
                "message": message,
                "distinct": True,
                "url": (
                    f"https://api.github.com/repos/{SUBJECT}/project-alpha/commits/"
                    f"{f'{index + 1:x}' * 40}"
                ),
            }
            for index, message in enumerate(COMMIT_MESSAGES)
        ],
    }
    return [event]


def main() -> None:
    subject = json.loads((CAPTURES / "user.json").read_text(encoding="utf-8-sig"))["login"]

    user = _swap_identity(
        json.loads((CAPTURES / "user.json").read_text(encoding="utf-8-sig")),
        subject,
        {},
        {},
        {},
    )
    user["name"] = "Test Subject"
    user["company"] = "Acme Corp"
    user["location"] = "Portland, OR"
    (HERE / "github_user.json").write_text(json.dumps(user, indent=2) + "\n", encoding="utf-8")

    events = scrub_events(
        json.loads((CAPTURES / "events.json").read_text(encoding="utf-8-sig")), subject
    )
    (HERE / "github_events.json").write_text(json.dumps(events, indent=2) + "\n", encoding="utf-8")

    push_template = next(e for e in events if e["type"] == "PushEvent")
    (HERE / "github_events_with_commits.json").write_text(
        json.dumps(synthetic_push_with_commits(push_template), indent=2) + "\n", encoding="utf-8"
    )

    search = scrub_search(
        json.loads((CAPTURES / "pr_comments.json").read_text(encoding="utf-8-sig")), subject
    )
    (HERE / "github_search_pr.json").write_text(
        json.dumps(search, indent=2) + "\n", encoding="utf-8"
    )
    print("wrote 4 github fixtures")


if __name__ == "__main__":
    main()
