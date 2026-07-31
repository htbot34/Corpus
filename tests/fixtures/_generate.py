"""Generate the fixture corpus.

NOTE ON PROVENANCE: these fixtures are SYNTHETIC. The intended workflow from
the build order is to ingest ~50 posts from one real account and snapshot the
raw responses here. That could not be done in the environment this was built in
(api.twitterapi.io is blocked by network policy and no X_API_KEY was present),
so the shapes below are written from twitterapi.io's documented response format
rather than captured from the wire.

Everything downstream iterates offline against these. When you have a key, run:

    corpus run --x <handle> --max-posts 50 --budget 0.10 --skip-synthesis

then copy the cached raw payloads over these files. `corpus/x/client.py`
(`normalize_tweet`) is the only place that needs to change if the real shapes
differ.

Run: python tests/fixtures/_generate.py
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
HANDLE = "testsubject"
AUTHOR_ID = "1000000000000000001"

START = datetime(2024, 1, 8, 15, 30, tzinfo=timezone.utc)


def fmt(dt: datetime) -> str:
    # twitterapi.io returns X's native format: "Tue Dec 10 07:00:30 +0000 2024"
    return dt.strftime("%a %b %d %H:%M:%S %z %Y")


def author(handle: str = HANDLE, name: str = "Test Subject", uid: str = AUTHOR_ID) -> dict:
    return {
        "type": "user",
        "userName": handle,
        "id": uid,
        "name": name,
        "isVerified": False,
        "followers": 42000,
        "following": 900,
    }


def tweet(
    tid: str,
    when: datetime,
    text: str,
    *,
    likes: int = 20,
    replies: int = 3,
    reposts: int = 2,
    quotes: int = 0,
    views: int = 4000,
    in_reply_to: str | None = None,
    in_reply_to_user: str | None = None,
    quoted: dict | None = None,
    retweeted: dict | None = None,
    urls: list[tuple[str, str]] | None = None,
    media: bool = False,
    handle: str = HANDLE,
) -> dict:
    entities: dict = {}
    if urls:
        entities["urls"] = [
            {"url": short, "expanded_url": expanded, "display_url": expanded[:30]}
            for short, expanded in urls
        ]
    payload = {
        "type": "tweet",
        "id": tid,
        "url": f"https://x.com/{handle}/status/{tid}",
        "text": text,
        "source": "Twitter Web App",
        "retweetCount": reposts,
        "replyCount": replies,
        "likeCount": likes,
        "quoteCount": quotes,
        "viewCount": views,
        "bookmarkCount": 1,
        "createdAt": fmt(when),
        "lang": "en",
        "isReply": bool(in_reply_to),
        "conversationId": in_reply_to or tid,
        "author": author(handle) if handle == HANDLE else author(handle, handle, "2" + tid),
        "entities": entities,
    }
    if in_reply_to:
        payload["inReplyToId"] = in_reply_to
        payload["inReplyToUsername"] = in_reply_to_user or HANDLE
        payload["inReplyToUserId"] = "9" + in_reply_to
    if quoted:
        payload["quoted_tweet"] = quoted
    if retweeted:
        payload["retweeted_tweet"] = retweeted
    if media:
        payload["extendedEntities"] = {
            "media": [{"type": "photo", "media_url_https": "https://pbs.twimg.com/media/x.jpg"}]
        }
    return payload


def other(tid: str, when: datetime, handle: str, text: str) -> dict:
    return tweet(tid, when, text, handle=handle, likes=8, replies=1, reposts=0, views=900)


def build() -> tuple[list[dict], list[dict]]:
    """Returns (subject tweets, other-authored tweets used as hydration targets)."""
    subject: list[dict] = []
    parents: list[dict] = []
    t = START

    def step(days: float = 1.0) -> datetime:
        nonlocal t
        t = t + timedelta(days=days)
        return t

    # --- a 4-part thread: the actual argument ---------------------------
    root = "1700000000000000101"
    subject.append(
        tweet(
            root,
            step(0),
            "Most hiring processes optimize for the wrong thing. They select for "
            "people who are good at interviews, which is a different skill from "
            "being good at the job. A thread on what we changed.",
            likes=1840,
            replies=96,
            reposts=310,
            views=284000,
        )
    )
    subject.append(
        tweet(
            "1700000000000000102",
            step(0.01),
            "First: we stopped asking algorithm puzzles. Nobody on our team has "
            "written a red-black tree in six years. We now ship a scoped, paid "
            "two-day project drawn from a real ticket we already closed.",
            in_reply_to=root,
            likes=420,
            replies=18,
            reposts=44,
            views=61000,
        )
    )
    subject.append(
        tweet(
            "1700000000000000103",
            step(0.01),
            "Second: the person who would manage the hire writes the rubric before "
            "anyone interviews. If you cannot write down what good looks like, you "
            "are not hiring, you are auditioning.",
            in_reply_to="1700000000000000102",
            likes=380,
            replies=12,
            reposts=51,
            views=54000,
        )
    )
    subject.append(
        tweet(
            "1700000000000000104",
            step(0.01),
            "Result after 18 months: time-to-productive dropped from 11 weeks to 4, "
            "and first-year attrition went from 22% to 6%. Small sample. Still, we "
            "are not going back.",
            in_reply_to="1700000000000000103",
            likes=505,
            replies=31,
            reposts=88,
            views=77000,
        )
    )

    # --- originals with links (reading diet) -----------------------------
    subject.append(
        tweet(
            "1700000000000000110",
            step(3),
            "This paper on organizational slack is the best thing I have read this "
            "year. The finding that slack predicts recovery speed better than "
            "headcount is not intuitive. https://t.co/aaa111",
            urls=[("https://t.co/aaa111", "https://www.nature.com/articles/s41586-024-00001")],
            likes=210,
            replies=14,
            reposts=38,
            views=31000,
        )
    )
    subject.append(
        tweet(
            "1700000000000000111",
            step(2),
            "Reminder that 'we tried that and it didn't work' usually means 'we tried "
            "it for six weeks with one skeptical person assigned part time'.",
            likes=980,
            replies=42,
            reposts=176,
            views=142000,
        )
    )
    subject.append(
        tweet(
            "1700000000000000112",
            step(4),
            "Good writeup on incident review culture. https://t.co/bbb222",
            urls=[("https://t.co/bbb222", "https://increment.com/reliability/incident-review/")],
            likes=88,
            replies=5,
            reposts=12,
            views=14000,
        )
    )

    # --- replies to others: where the arguing happens --------------------
    p1 = "1700000000000000201"
    parents.append(
        other(
            p1,
            step(1),
            "criticfriend",
            "Paid work trials are a tax on candidates who already have jobs. They "
            "systematically favor the unemployed and the young.",
        )
    )
    subject.append(
        tweet(
            "1700000000000000120",
            step(0.02),
            "That is the strongest objection and I do not have a clean answer. What we "
            "do: the project is two days, paid at contractor rate, and can be done "
            "across two weekends. It still filters out people with caregiving loads. "
            "I think that cost is real and we accept it knowingly.",
            in_reply_to=p1,
            in_reply_to_user="criticfriend",
            likes=340,
            replies=22,
            reposts=29,
            views=48000,
        )
    )

    p2 = "1700000000000000202"
    parents.append(
        other(
            p2,
            step(2),
            "vcpartner",
            "Founders should hire ahead of the curve. If you wait until you need the "
            "role filled you are already six months late.",
        )
    )
    subject.append(
        tweet(
            "1700000000000000121",
            step(0.03),
            "Completely backwards for anyone under 30 people. Hiring ahead of the curve "
            "is how you end up with a org chart that describes a company you are not "
            "yet. Hire when the pain is measurable and specific.",
            in_reply_to=p2,
            in_reply_to_user="vcpartner",
            likes=1120,
            replies=64,
            reposts=203,
            views=198000,
        )
    )

    p3 = "1700000000000000203"
    parents.append(
        other(
            p3,
            step(5),
            "engmanager",
            "How do you handle the case where the rubric writer leaves before the hire "
            "ramps up?",
        )
    )
    subject.append(
        tweet(
            "1700000000000000122",
            step(0.02),
            "Then the rubric is the handoff document, which is most of the value. We "
            "keep them in the repo next to the ADRs.",
            in_reply_to=p3,
            in_reply_to_user="engmanager",
            likes=64,
            replies=3,
            reposts=6,
            views=9000,
        )
    )

    # --- a quote tweet ---------------------------------------------------
    q1 = "1700000000000000301"
    quoted_payload = other(
        q1,
        step(3),
        "bigcoexec",
        "We are mandating five days in office because collaboration requires presence.",
    )
    parents.append(quoted_payload)
    subject.append(
        tweet(
            "1700000000000000130",
            step(0.05),
            "Every RTO announcement I have read asserts the mechanism instead of "
            "measuring it. Show me the before-and-after on shipped work, not the "
            "vibe about whiteboards.",
            quoted=quoted_payload,
            likes=1560,
            replies=88,
            reposts=402,
            views=310000,
        )
    )

    # --- a repost (excluded by default) ----------------------------------
    rt = other(
        "1700000000000000401",
        step(2),
        "researchfriend",
        "New replication: the open-office productivity effect is negative and larger "
        "than the original estimate.",
    )
    subject.append(
        tweet(
            "1700000000000000140",
            step(0.01),
            "RT @researchfriend: New replication on open offices",
            retweeted=rt,
            likes=0,
            replies=0,
            reposts=61,
            views=0,
        )
    )

    # --- media-only (excluded from synthesis, counted in cadence) --------
    subject.append(
        tweet(
            "1700000000000000150",
            step(1),
            "this",
            media=True,
            likes=45,
            replies=2,
            reposts=3,
            views=8000,
        )
    )

    # --- later period: vocabulary drift + a view change ------------------
    subject.append(
        tweet(
            "1700000000000000160",
            step(160),
            "Updating on work trials: we replaced the two-day project with a paid "
            "one-week contract for senior roles. The two-day version measured "
            "sprinting, not judgment. I was wrong about how much you can learn in 48 "
            "hours.",
            likes=890,
            replies=57,
            reposts=134,
            views=121000,
        )
    )
    subject.append(
        tweet(
            "1700000000000000161",
            step(4),
            "Evaluation design is the whole job now. Every agent workflow we run is "
            "bottlenecked on whether we can tell if the output was good, not on "
            "whether the model can produce it.",
            likes=1240,
            replies=71,
            reposts=228,
            views=204000,
        )
    )
    subject.append(
        tweet(
            "1700000000000000162",
            step(3),
            "Good taxonomy of evaluation failure modes here. https://t.co/ccc333",
            urls=[("https://t.co/ccc333", "https://arxiv.org/abs/2502.00001")],
            likes=176,
            replies=9,
            reposts=41,
            views=27000,
        )
    )

    p4 = "1700000000000000204"
    parents.append(
        other(
            p4,
            step(2),
            "criticfriend",
            "You have quietly become an evals person. Where did the hiring stuff go?",
        )
    )
    subject.append(
        tweet(
            "1700000000000000163",
            step(0.02),
            "Same problem wearing a different hat. Hiring rubrics and model evals are "
            "both 'write down what good looks like before you look at the output'. I "
            "just found a domain where people will actually pay for the rubric.",
            in_reply_to=p4,
            in_reply_to_user="criticfriend",
            likes=760,
            replies=34,
            reposts=97,
            views=98000,
        )
    )
    subject.append(
        tweet(
            "1700000000000000164",
            step(6),
            "Evaluation harnesses rot faster than the systems they evaluate. Budget for "
            "maintaining them or they become a compliance ritual within two quarters.",
            likes=430,
            replies=19,
            reposts=76,
            views=63000,
        )
    )
    subject.append(
        tweet(
            "1700000000000000165",
            step(21),
            "Six months of running evals in anger: the hard part is never the metric, "
            "it is agreeing on the ground truth set. https://t.co/ddd444",
            urls=[("https://t.co/ddd444", "https://arxiv.org/abs/2503.09999")],
            likes=612,
            replies=28,
            reposts=118,
            views=88000,
        )
    )

    # a deleted parent — hydration must degrade to [unavailable], not drop
    subject.append(
        tweet(
            "1700000000000000170",
            step(2),
            "No, the point is that the baseline was never measured, so the comparison "
            "is doing no work.",
            in_reply_to="1700000000000000999",  # never returned by the provider
            in_reply_to_user="ghostaccount",
            likes=52,
            replies=4,
            reposts=5,
            views=7200,
        )
    )

    subject.sort(key=lambda x: x["id"])
    return subject, parents


def main() -> None:
    subject, parents = build()

    (HERE / "tweets.json").write_text(json.dumps(subject, indent=2), encoding="utf-8")
    (HERE / "parents.json").write_text(json.dumps(parents, indent=2), encoding="utf-8")

    # One captured-shape example of each endpoint's envelope.
    (HERE / "search_response.json").write_text(
        json.dumps(
            {
                "tweets": subject[:3],
                "has_next_page": True,
                "next_cursor": "DAABCgABGf__cursor__page2",
                "status": "success",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (HERE / "last_tweets_response.json").write_text(
        json.dumps(
            {
                "status": "success",
                "data": {"tweets": subject[:3], "pin_tweet": None},
                "has_next_page": True,
                "next_cursor": "DAABCgABGf__cursor__page2",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (HERE / "batch_tweets_response.json").write_text(
        json.dumps({"tweets": parents[:2], "status": "success"}, indent=2), encoding="utf-8"
    )
    # user_info is the ONE endpoint whose shape is confirmed against the live
    # API (2026-07-31, GET /twitter/user/info?userName=paulg). Values below are
    # still synthetic; the key set, the envelope, and the timestamp format are
    # not. Two things this corrected that the documented-shape guess got wrong:
    #
    #   - "msg" is present in the envelope. The old fixture omitted it.
    #   - createdAt is ISO 8601 with six-digit microseconds and a Z suffix,
    #     not the legacy "Mon Mar 03 12:00:00 +0000 2014" form. parse_created_at
    #     only survives it via its fromisoformat fallback — see Phase 2.6.
    #
    # Field order matches the live response. Note the British "favouritesCount".
    (HERE / "user_info.json").write_text(
        json.dumps(
            {
                "status": "success",
                "msg": "success",
                "data": {
                    "id": AUTHOR_ID,
                    "name": "Test Subject",
                    "userName": HANDLE,
                    "location": "",
                    "url": None,
                    "description": "Writes about hiring, evaluation, and organizations.",
                    "entities": {
                        "description": {"urls": []},
                        "url": {"urls": []},
                    },
                    "protected": False,
                    "isVerified": False,
                    "isBlueVerified": False,
                    "verifiedType": None,
                    "followers": 42000,
                    "following": 900,
                    "favouritesCount": 15340,
                    "statusesCount": 4820,
                    "mediaCount": 210,
                    "createdAt": "2014-03-03T12:00:00.000000Z",
                    "coverPicture": "",
                    "profilePicture": "",
                    "canDm": False,
                    "affiliatesHighlightedLabel": {},
                    "isAutomated": False,
                    "automatedBy": None,
                    "pinnedTweetIds": [],
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {len(subject)} subject tweets and {len(parents)} hydration targets to {HERE}")


if __name__ == "__main__":
    main()
