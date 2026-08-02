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

# Long enough to clear MIN_REASONING_CHARS and shaped like an actual chain, so
# the fixtures exercise the "kept" branch of the inference enforcement.
GOOD_CHAIN = (
    "They twice answer institutional-authority claims with a counterexample from "
    "their own hiring data rather than disputing the study, which places the "
    "burden of proof on the institution rather than on the dissenter."
)


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
                "reasoning_moves": [
                    {"move": "concedes then narrows", "example_id": ids[0] if ids else ""}
                ],
                "highest_signal_document_ids": ids[:3],
            }
        )

    def _default_reduce(self, kwargs: dict[str, Any]) -> str:
        ids = self._ids_in(kwargs)
        real = ids[:2] or ["1700000000000000101"]
        return json.dumps(
            {
                "summary": "One. Two. Three. Four.",
                "core_model": [
                    {
                        "belief": (
                            "Process quality is measurable and most people refuse to measure it"
                        ),
                        "role": "load_bearing",
                        "generates": [
                            "hostility to interview puzzles",
                            "preference for work trials",
                        ],
                        "evidence_ids": real,
                    },
                    {
                        "belief": "invented belief",
                        "role": "derived",
                        "generates": [],
                        "evidence_ids": ["9999999999"],  # not in the corpus
                    },
                ],
                "reasoning": {
                    "moves": [
                        {"move": "concedes the strongest objection first", "example_id": real[0]},
                        {"move": "invented move", "example_id": "9999999999"},
                    ],
                    "what_counts_as_evidence": "Numbers they gathered themselves.",
                    "under_disagreement": "Grants the point, then narrows it.",
                    "updates_when": "Someone shows them a measurement they cannot dispute.",
                    "blind_spots": [
                        {
                            "pattern": "Treats their own sample as representative",
                            "basis": GOOD_CHAIN,
                            "evidence_ids": real,
                        },
                        {
                            "pattern": "hand-waved blind spot",
                            "basis": "seems likely",
                            "evidence_ids": real,
                        },
                    ],
                },
                "axes": [
                    {
                        "axis": "institutions_and_authority",
                        "signal": "strong",
                        "stated": "Credentials are a lazy proxy for competence.",
                        "inferred": "They treat institutional legitimacy as earned per-claim.",
                        "reasoning": GOOD_CHAIN,
                        "confidence": "medium",
                        "evidence_ids": real,
                    },
                    {
                        "axis": "epistemics",
                        "signal": "weak",
                        "stated": "Prefers measurement to intuition.",
                        "inferred": "They are a naive empiricist.",
                        "reasoning": "it follows",  # hand-waving: must be dropped
                        "confidence": "high",
                        "evidence_ids": real,
                    },
                    {
                        "axis": "defense_intel_natsec",
                        "signal": "none",
                        "stated": "",
                        "inferred": "",
                        "reasoning": "",
                        "confidence": "low",
                        "evidence_ids": [],
                    },
                ],
                "evolution": [
                    {
                        "topic": "work trials",
                        "earlier": "two days is enough",
                        "later": "two days measures sprinting",
                        "inflection": "2024-06-15",
                        "evidence_ids": real,
                    }
                ],
                "open_questions": [
                    {
                        "question": "How to price work trials fairly",
                        "returned_to": 2,
                        "evidence_ids": real,
                    }
                ],
                "misreadings": [
                    {
                        "misreading": "That they are anti-credential in general",
                        "why_wrong": "They defend credentials where the measurement is real.",
                        "evidence_ids": real,
                    }
                ],
                "coverage": {
                    "date_range": "2024-01-08 to 2024-08-01",
                    "total_documents": len(ids),
                    "gaps": [],
                    "confidence": "medium",
                },
            }
        )
