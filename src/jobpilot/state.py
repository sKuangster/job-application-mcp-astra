"""Explicit application lifecycle, independent of transport and persistence."""

from enum import StrEnum


class ApplicationState(StrEnum):
    DISCOVERED = "DISCOVERED"
    QUEUED = "QUEUED"
    CHECKING_ELIGIBILITY = "CHECKING_ELIGIBILITY"
    INELIGIBLE = "INELIGIBLE"
    READY = "READY"
    OPENING = "OPENING"
    APPLYING = "APPLYING"
    ACCOUNT_REQUIRED = "ACCOUNT_REQUIRED"
    VERIFYING_EMAIL = "VERIFYING_EMAIL"
    FILLING = "FILLING"
    ANSWERING = "ANSWERING"
    NEEDS_USER = "NEEDS_USER"
    VALIDATING = "VALIDATING"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    SUBMISSION_UNCONFIRMED = "SUBMISSION_UNCONFIRMED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


State = ApplicationState

TERMINAL_STATES = frozenset(
    {"INELIGIBLE", "SUBMITTED", "SUBMISSION_UNCONFIRMED", "FAILED", "SKIPPED"}
)
PARKED_STATES = TERMINAL_STATES | {"NEEDS_USER"}
SUBMISSION_STATES = frozenset({"SUBMITTING", "SUBMITTED", "SUBMISSION_UNCONFIRMED"})

_FLOW: dict[str, set[str]] = {
    "DISCOVERED": {"QUEUED"},
    "QUEUED": {"CHECKING_ELIGIBILITY", "READY", "OPENING"},
    "CHECKING_ELIGIBILITY": {"INELIGIBLE", "READY", "OPENING"},
    "READY": {"CHECKING_ELIGIBILITY", "OPENING", "APPLYING", "FILLING"},
    "OPENING": {
        "CHECKING_ELIGIBILITY",
        "APPLYING",
        "ACCOUNT_REQUIRED",
        "FILLING",
        "ANSWERING",
        "VALIDATING",
    },
    "APPLYING": {"ACCOUNT_REQUIRED", "VERIFYING_EMAIL", "FILLING", "ANSWERING", "VALIDATING"},
    "ACCOUNT_REQUIRED": {"VERIFYING_EMAIL", "OPENING", "APPLYING", "FILLING"},
    "VERIFYING_EMAIL": {"OPENING", "APPLYING", "FILLING"},
    "FILLING": {"APPLYING", "ACCOUNT_REQUIRED", "VERIFYING_EMAIL", "ANSWERING", "VALIDATING"},
    "ANSWERING": {"APPLYING", "FILLING", "VALIDATING"},
    "VALIDATING": {"FILLING", "ANSWERING", "APPLYING", "SUBMITTING"},
    "NEEDS_USER": {
        "QUEUED",
        "READY",
        "OPENING",
        "APPLYING",
        "ACCOUNT_REQUIRED",
        "VERIFYING_EMAIL",
        "FILLING",
        "ANSWERING",
        "VALIDATING",
    },
    "SUBMITTING": {"SUBMITTED", "SUBMISSION_UNCONFIRMED"},
    "SUBMITTED": set(),
    "SUBMISSION_UNCONFIRMED": {"SUBMITTED"},
    "INELIGIBLE": {"QUEUED"},
    "FAILED": {"QUEUED", "READY"},
    "SKIPPED": {"QUEUED"},
}
for _state in _FLOW:
    if _state not in TERMINAL_STATES and _state != "SUBMITTING":
        _FLOW[_state].update({"NEEDS_USER", "FAILED", "SKIPPED"})

ALLOWED_TRANSITIONS = {state: frozenset(targets) for state, targets in _FLOW.items()}


class InvalidTransition(ValueError):
    """A requested transition violates the lifecycle."""


class LeaseLost(RuntimeError):
    """A worker no longer owns an unexpired application lease."""


def validate_transition(from_state: str, to_state: str) -> None:
    if from_state not in ALLOWED_TRANSITIONS or to_state not in ALLOWED_TRANSITIONS:
        raise InvalidTransition(f"Unknown application state: {from_state!r} -> {to_state!r}")
    if from_state != to_state and to_state not in ALLOWED_TRANSITIONS[from_state]:
        raise InvalidTransition(f"Application cannot transition from {from_state} to {to_state}")


def can_transition(from_state: str, to_state: str) -> bool:
    try:
        validate_transition(from_state, to_state)
    except InvalidTransition:
        return False
    return True
