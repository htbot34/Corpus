"""The grammar-size guard.

The API compiles `output_config.format.schema` into a decoding grammar and
rejects anything too large with a 400: "The compiled grammar is too large."
The ceiling was bisected against the live API: 3,522 bytes accepted, 3,809
rejected.

Exceeding it does not fail loudly at runtime. The call errors, the code falls
back to prompt-guided JSON, and the run keeps going — slower, retry-prone, and
occasionally wrapping its output in markdown fences that burn a full billed
generation. A future field addition should fail this test, not degrade
production silently.

3,400 leaves headroom under the last known-good 3,522 without pretending we
know the exact boundary.
"""

from __future__ import annotations

import json

from corpus.synthesize import MAP_SCHEMA, REDUCE_SCHEMA, schema_bytes

GRAMMAR_LIMIT = 3_400
LAST_KNOWN_ACCEPTED = 3_522
FIRST_KNOWN_REJECTED = 3_809


def test_reduce_schema_fits_the_grammar_limit():
    size = schema_bytes(REDUCE_SCHEMA)
    assert size <= GRAMMAR_LIMIT, (
        f"REDUCE_SCHEMA is {size} bytes, over the {GRAMMAR_LIMIT}-byte budget. "
        f"The live API accepted {LAST_KNOWN_ACCEPTED} and rejected "
        f"{FIRST_KNOWN_REJECTED}. Over the limit, constrained decoding is "
        "refused and every run silently drops to the slower fallback path. "
        "Remove or merge a field rather than raising this number."
    )


def test_map_schema_fits_the_grammar_limit():
    assert schema_bytes(MAP_SCHEMA) <= GRAMMAR_LIMIT


def test_schema_bytes_matches_how_the_limit_was_measured():
    """The bisection used default json.dumps separators. Measure the same way,
    or the budget above is compared against a different number."""
    assert schema_bytes(REDUCE_SCHEMA) == len(json.dumps(REDUCE_SCHEMA))


def test_every_schema_object_is_closed():
    """A schema that allows extra properties is not actually constraining the
    output, which is the entire point of paying the grammar cost."""

    def walk(node, path="$"):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, f"{path} is open"
                assert set(node.get("required", [])) == set(node.get("properties", {})), (
                    f"{path}: every property must be required"
                )
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(REDUCE_SCHEMA)
    walk(MAP_SCHEMA)
