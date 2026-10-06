"""Explicit application state machine.

The workflow is a set of persisted states with an allow-list of transitions. The
engine runs one *handler* per state (see engine/runner.py); handlers return the
next state, so the workflow is never a pile of nested conditionals and a restart
resumes from the last persisted state.
"""

from __future__ import annotations

from enum import StrEnum


class S(StrEnum):
    DISCOVERED = "DISCOVERED"
    QUEUED = "QUEUED"
    CHECKING_ELIGIBILITY = "CHECKING_ELIGIBILITY"  # prep: Simplify page, eligibility, resume
    INELIGIBLE = "INELIGIBLE"
    READY = "READY"  # prepared, waiting for a browser worker
    OPENING = "OPENING"  # opening owned tab at the application URL
    APPLYING = "APPLYING"  # classifying the current page and choosing the next move
    ACCOUNT_REQUIRED = "ACCOUNT_REQUIRED"
    VERIFYING_EMAIL = "VERIFYING_EMAIL"
    FILLING = "FILLING"
    ANSWERING = "ANSWERING"  # parked until Astra answers semantic questions
    WAITING_FOR_CAPTCHA = "WAITING_FOR_CAPTCHA"  # parked; watcher scans without interacting
    NEEDS_USER = "NEEDS_USER"
    VALIDATING = "VALIDATING"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"  # parked at the final page when auto-submit is off
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    SUBMISSION_UNCONFIRMED = "SUBMISSION_UNCONFIRMED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


TERMINAL = frozenset({S.INELIGIBLE, S.SUBMITTED, S.SUBMISSION_UNCONFIRMED, S.FAILED, S.SKIPPED})
# Parked states are not claimable by workers until something external changes them.
PARKED = TERMINAL | {S.WAITING_FOR_CAPTCHA, S.NEEDS_USER, S.READY_TO_SUBMIT, S.DISCOVERED}
PREP_STATES = frozenset({S.QUEUED, S.CHECKING_ELIGIBILITY})
BROWSER_STATES = frozenset(
    {S.READY, S.OPENING, S.APPLYING, S.ACCOUNT_REQUIRED, S.VERIFYING_EMAIL, S.FILLING,
     S.ANSWERING, S.VALIDATING, S.SUBMITTING}
)
# States in which the application holds a browser tab (including while parked).
TAB_HOLDING = (BROWSER_STATES - {S.READY}) | {S.WAITING_FOR_CAPTCHA, S.NEEDS_USER, S.READY_TO_SUBMIT}
ACTIVE = PREP_STATES | BROWSER_STATES
# A crash in these states must never cause an automatic re-click of submit.
SUBMISSION = frozenset({S.SUBMITTING, S.SUBMITTED, S.SUBMISSION_UNCONFIRMED})

_page_states = {S.APPLYING, S.ACCOUNT_REQUIRED, S.VERIFYING_EMAIL, S.FILLING, S.ANSWERING, S.VALIDATING}

_FLOW: dict[S, set[S]] = {
    S.DISCOVERED: {S.QUEUED, S.SKIPPED},
    S.QUEUED: {S.CHECKING_ELIGIBILITY, S.READY},
    S.CHECKING_ELIGIBILITY: {S.INELIGIBLE, S.READY, S.QUEUED},
    S.READY: {S.OPENING, S.CHECKING_ELIGIBILITY},
    S.OPENING: {S.APPLYING, S.OPENING},
    S.APPLYING: _page_states | {S.OPENING, S.READY_TO_SUBMIT, S.SUBMITTING},
    S.ACCOUNT_REQUIRED: _page_states | {S.OPENING},
    S.VERIFYING_EMAIL: _page_states | {S.OPENING},
    S.FILLING: _page_states | {S.OPENING},
    S.ANSWERING: _page_states | {S.OPENING},
    S.WAITING_FOR_CAPTCHA: {S.OPENING, S.APPLYING, S.FILLING, S.VALIDATING, S.SUBMITTING,
                            S.SUBMITTED, S.SUBMISSION_UNCONFIRMED},
    S.VALIDATING: _page_states | {S.OPENING, S.READY_TO_SUBMIT, S.SUBMITTING},
    S.READY_TO_SUBMIT: {S.SUBMITTING, S.APPLYING, S.VALIDATING, S.FILLING, S.OPENING},
    S.NEEDS_USER: {S.QUEUED, S.CHECKING_ELIGIBILITY, S.READY, S.OPENING} | _page_states
    | {S.READY_TO_SUBMIT, S.SUBMITTING, S.SUBMITTED, S.SUBMISSION_UNCONFIRMED},
    S.SUBMITTING: {S.SUBMITTED, S.SUBMISSION_UNCONFIRMED, S.FILLING, S.VALIDATING, S.APPLYING, S.OPENING},
    S.SUBMITTED: set(),
    S.SUBMISSION_UNCONFIRMED: {S.SUBMITTED},
    S.INELIGIBLE: {S.QUEUED},
    S.FAILED: {S.QUEUED, S.READY},
    S.SKIPPED: {S.QUEUED},
}
# Any non-terminal, non-submitting state may park for the user, fail or be skipped.
for _state, _targets in _FLOW.items():
    if _state not in TERMINAL and _state != S.SUBMITTING:
        _targets.update({S.WAITING_FOR_CAPTCHA, S.NEEDS_USER, S.FAILED, S.SKIPPED})
_FLOW[S.SUBMITTING].update({S.WAITING_FOR_CAPTCHA, S.NEEDS_USER, S.FAILED})

ALLOWED: dict[str, frozenset[str]] = {
    state.value: frozenset(t.value for t in targets) for state, targets in _FLOW.items()
}

# Claim priority: lower runs first. Resuming in-flight work beats starting new work,
# prepared jobs beat unprepared ones (spec §50).
CLAIM_PRIORITY = {
    S.SUBMITTING: 0, S.VALIDATING: 1, S.ANSWERING: 1, S.FILLING: 2, S.VERIFYING_EMAIL: 2,
    S.ACCOUNT_REQUIRED: 2, S.APPLYING: 3, S.OPENING: 3, S.READY: 5, S.CHECKING_ELIGIBILITY: 6,
    S.QUEUED: 7,
}


class InvalidTransition(ValueError):
    """A requested transition violates the lifecycle."""


class LeaseLost(RuntimeError):
    """The worker no longer owns the application's lease (fencing)."""


def validate_transition(src: str, dst: str) -> None:
    if src not in ALLOWED or dst not in ALLOWED:
        raise InvalidTransition(f"Unknown state in {src!r} -> {dst!r}")
    if src != dst and dst not in ALLOWED[src]:
        raise InvalidTransition(f"Illegal transition {src} -> {dst}")


def can_transition(src: str, dst: str) -> bool:
    try:
        validate_transition(src, dst)
    except InvalidTransition:
        return False
    return True
