"""A stub Anthropic client so synthesis can be iterated on without paying.

It mimics the shape the code actually touches: `messages.create(...)`,
`messages.stream(...)` as an async context manager with `get_final_message()`,
and responses carrying `.content`, `.usage`, `.stop_reason`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Block:
    text: str
    type: str = "text"


@dataclass
class _Usage:
    input_tokens: int = 1000
    output_tokens: int = 300
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _Message:
    content: list[_Block]
    usage: _Usage = field(default_factory=_Usage)
    stop_reason: str = "end_turn"


class _Stream:
    def __init__(self, message: _Message) -> None:
        self._message = message

    async def __aenter__(self) -> _Stream:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get_final_message(self) -> _Message:
        return self._message


class _Messages:
    def __init__(self, owner: FakeAnthropic) -> None:
        self.owner = owner

    async def create(self, **kwargs: Any) -> _Message:
        self.owner.calls.append(kwargs)
        return _Message([_Block(self.owner.map_response(kwargs))])

    def stream(self, **kwargs: Any) -> _Stream:
        self.owner.calls.append(kwargs)
        return _Stream(_Message([_Block(self.owner.reduce_response(kwargs))]))


class FakeAnthropic:
    def __init__(
        self,
        map_response: Callable[[dict[str, Any]], str] | None = None,
        reduce_response: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.messages = _Messages(self)
        self.map_response = map_response or self._default_map
        self.reduce_response = reduce_response or self._default_reduce

    async def close(self) -> None:
        self.closed = True

    # -- default canned responses ----------------------------------------

    @staticmethod
    def _ids_in(kwargs: dict[str, Any]) -> list[str]:
        text = json.dumps(kwargs.get("messages", []), default=str)
        out: list[str] = []
        marker = "[id: "
        start = 0
        while True:
            i = text.find(marker, start)
            if i < 0:
                break
            j = text.find("]", i)
            out.append(text[i + len(marker) : j])
            start = j
        return out

    def _default_map(self, kwargs: dict[str, Any]) -> str:
        ids = self._ids_in(kwargs)
        return json.dumps(
            {
                "topics": [{"name": "hiring process design", "document_ids": ids[:3]}],
                "claims": [
                    {
                        "claim": "Interview puzzles select for the wrong skill",
                        "document_ids": ids[:2],
                        "confidence": "stated",
                    }
                ],
                "entities": ["work trials"],
                "argumentative_moves": ["concedes then narrows"],
                "highest_signal_document_ids": ids[:3],
            }
        )

    def _default_reduce(self, kwargs: dict[str, Any]) -> str:
        ids = self._ids_in(kwargs)
        real = ids[:2] or ["1700000000000000101"]
        return json.dumps(
            {
                "summary": "One. Two. Three.",
                "themes": [
                    {
                        "name": "hiring process design",
                        "post_count": 6,
                        "share_of_corpus": 0.4,
                        "first_seen": "2024-01-08",
                        "last_seen": "2024-07-01",
                        "trajectory": "declining",
                        "evidence_ids": real,
                        "low_evidence": False,
                    },
                    {
                        "name": "invented theme",
                        "post_count": 1,
                        "share_of_corpus": 0.1,
                        "first_seen": "",
                        "last_seen": "",
                        "trajectory": "steady",
                        "evidence_ids": ["9999999999"],  # not in the corpus
                        "low_evidence": True,
                    },
                ],
                "positions": [
                    {
                        "claim": "Hire when the pain is specific, not ahead of the curve",
                        "confidence": "stated",
                        "evidence_ids": real,
                        "contradicted_by_ids": [],
                        "low_evidence": False,
                    }
                ],
                "argument_style": {
                    "typical_moves": ["concedes the strongest objection first"],
                    "how_they_handle_disagreement": "Grants the point, then narrows it.",
                    "evidence_ids": real,
                    "low_evidence": False,
                },
                "network": [
                    {
                        "handle": "criticfriend",
                        "exchange_count": 2,
                        "relationship": "sparring partner",
                        "evidence_ids": real,
                    }
                ],
                "reading_diet": [
                    {"domain": "arxiv.org", "share_count": 2, "what_it_suggests": "reads papers"}
                ],
                "evolution": [
                    {
                        "topic": "work trials",
                        "earlier_view": "two days is enough",
                        "later_view": "two days measures sprinting",
                        "inflection_date": "2024-06-15",
                        "evidence_ids": real,
                        "low_evidence": False,
                    }
                ],
                "performance_gap": {
                    "posts_most_about": "hiring",
                    "gets_most_traction_on": "RTO criticism",
                    "interpretation": "The audience wants the fight, not the method.",
                    "evidence_ids": real,
                    "low_evidence": False,
                },
                "open_loops": [
                    {
                        "question": "How to price work trials fairly",
                        "returned_to_count": 2,
                        "evidence_ids": real,
                    }
                ],
                "voice": {
                    "register": "declarative, numbers-forward",
                    "hobbyhorses": ["rubrics"],
                    "tells": ["'completely backwards'"],
                },
                "hooks": [
                    {
                        "opener": "You said two-day trials measure sprinting, not judgment",
                        "anchor_url": "https://x.com/testsubject/status/1700000000000000160",
                        "why_it_works": "quotes their own reversal",
                    }
                ],
                "avoid": [
                    {
                        "topic_or_framing": "hiring ahead of the curve",
                        "reason": "called it completely backwards",
                        "evidence_ids": real,
                    }
                ],
                "coverage": {
                    "date_range": "2024-01-08 to 2024-08-01",
                    "total_documents": len(ids),
                    "kinds_included": ["original", "thread", "reply", "quote"],
                    "gaps": [],
                    "confidence": "medium",
                },
            }
        )
