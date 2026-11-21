from typing import Any


PUBLIC_INTERNAL_IDENTIFIER_KEYS = frozenset({"instructor_id", "client_id"})
PUBLIC_SCHEDULING_EVIDENCE_KEYS = frozenset(
    {
        "travel_time_mins",
        "buffer_mins",
        "distance_miles",
        "previous_event",
        "next_event",
        "safety_reason",
        "reason",
    }
)
PUBLIC_FORBIDDEN_KEYS = (
    PUBLIC_INTERNAL_IDENTIFIER_KEYS | PUBLIC_SCHEDULING_EVIDENCE_KEYS
)


def flatten_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(key)
            keys.update(flatten_keys(nested))
    elif isinstance(value, list):
        for item in value:
            keys.update(flatten_keys(item))
    return keys
