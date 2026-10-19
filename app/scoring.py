"""Job-specific scoring criteria: validation and the weighted overall.

Each job carries its own criteria — what matters for THIS role — instead of one rubric hardcoded for
a single company's internship. A criterion is ``{key, label, weight, description}``; the weights are
relative importances that the code normalizes. The overall is computed here, from the per-criterion
scores, never taken from whatever arithmetic a model produced.
"""
from __future__ import annotations

import re
from typing import Any

MAX_CRITERIA = 10
MIN_CRITERIA = 1
SCORE_MIN = 0.0
SCORE_MAX = 10.0

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class CriteriaError(ValueError):
    pass


def validate_criteria(raw: Any) -> list[dict]:
    """Return a normalized copy of ``raw`` or raise CriteriaError with a user-safe message."""
    if not isinstance(raw, list):
        raise CriteriaError("criteria must be a list")
    if not (MIN_CRITERIA <= len(raw) <= MAX_CRITERIA):
        raise CriteriaError(f"a job needs between {MIN_CRITERIA} and {MAX_CRITERIA} criteria")

    seen: set[str] = set()
    out: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise CriteriaError(f"criteria[{index}] must be an object")
        unknown = set(item) - {"key", "label", "weight", "description"}
        if unknown:
            raise CriteriaError(f"criteria[{index}] has unknown fields: {', '.join(sorted(unknown))}")
        key = str(item.get("key", "")).strip()
        if not _KEY_RE.match(key):
            raise CriteriaError(f"criteria[{index}].key must be lowercase letters, digits or _ (start with a letter)")
        if key in seen:
            raise CriteriaError(f"criteria key '{key}' is used twice")
        seen.add(key)
        label = str(item.get("label", "")).strip()
        if not (1 <= len(label) <= 80):
            raise CriteriaError(f"criteria[{index}].label must be 1-80 characters")
        weight = item.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise CriteriaError(f"criteria[{index}].weight must be a number")
        if not (1 <= weight <= 100):
            raise CriteriaError(f"criteria[{index}].weight must be between 1 and 100")
        description = str(item.get("description", "") or "").strip()
        if len(description) > 500:
            raise CriteriaError(f"criteria[{index}].description must be at most 500 characters")
        out.append({"key": key, "label": label, "weight": float(weight), "description": description})
    return out


def weighted_overall(criteria: list[dict], scores: dict[str, float]) -> float:
    """0-10 weighted mean over EVERY criterion. A missing score is an error, not a zero."""
    total_weight = sum(c["weight"] for c in criteria)
    if total_weight <= 0:
        raise CriteriaError("criteria weights sum to zero")
    acc = 0.0
    for c in criteria:
        if c["key"] not in scores:
            raise CriteriaError(f"no score for criterion '{c['key']}'")
        value = float(scores[c["key"]])
        if not (SCORE_MIN <= value <= SCORE_MAX):
            raise CriteriaError(f"score for '{c['key']}' is outside {SCORE_MIN}-{SCORE_MAX}")
        acc += value * c["weight"]
    return round(acc / total_weight, 2)
