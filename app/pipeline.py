"""Hiring pipeline rules. Pure functions — no database, no HTTP — so every rule is unit-testable.

Stages
------
``applied -> screening -> interview -> offer -> hired`` is the forward path.
``rejected`` and ``withdrawn`` are terminal exits available from any open stage.

A stage only ever changes because an authenticated person asked for it. Nothing automated — an AI
evaluation, an import, an inbound email — is allowed to move an application (see
``app/evaluation.py``); this module has no notion of "automatic" transitions on purpose.
"""
from __future__ import annotations

from dataclasses import dataclass

FORWARD = ("applied", "screening", "interview", "offer", "hired")
OPEN_STAGES = ("applied", "screening", "interview", "offer")
TERMINAL_EXITS = ("rejected", "withdrawn")
STAGES = FORWARD + TERMINAL_EXITS

# Legacy KukBook `candidates.cand_stage` values and what they map to. Anything not listed here is
# NOT guessed at: the importer quarantines it for a person to look at.
LEGACY_STAGE_MAP = {
    "new": "applied",
    "screening": "screening",
    "interview": "interview",
    "offer": "offer",
    "hired": "hired",
    "rejected": "rejected",
}


class TransitionError(ValueError):
    """The requested move is not allowed. The message is safe to show to the user."""


@dataclass(frozen=True)
class Transition:
    from_stage: str
    to_stage: str
    reopened: bool = False


def validate_transition(from_stage: str, to_stage: str, *, reason: str | None = None) -> Transition:
    if from_stage not in STAGES:
        raise TransitionError(f"Application is in an unknown stage '{from_stage}'.")
    if to_stage not in STAGES:
        raise TransitionError(f"'{to_stage}' is not a pipeline stage.")
    if from_stage == to_stage:
        raise TransitionError(f"Application is already in '{to_stage}'.")

    if from_stage == "hired":
        # A hire is final in KukHire. Reversing one is an employee-lifecycle decision that
        # belongs to HRMS, so recruitment does not offer it.
        raise TransitionError("A hired application cannot be moved. Reverse the hire in HRMS.")

    if from_stage in TERMINAL_EXITS:
        # Reopening a rejected/withdrawn application is allowed, back to the start, and only
        # with a reason so the history explains it.
        if to_stage != "applied":
            raise TransitionError("A closed application can only be reopened to 'applied'.")
        if not (reason or "").strip():
            raise TransitionError("A reason is required to reopen a closed application.")
        return Transition(from_stage, to_stage, reopened=True)

    if to_stage in TERMINAL_EXITS:
        return Transition(from_stage, to_stage)

    if to_stage == "hired":
        if from_stage != "offer":
            raise TransitionError("An application can only be hired from the 'offer' stage.")
        return Transition(from_stage, to_stage)

    # open -> open: forward (skipping allowed, up to offer) or back (to correct a mistake).
    return Transition(from_stage, to_stage)


def is_open(stage: str) -> bool:
    return stage in OPEN_STAGES
