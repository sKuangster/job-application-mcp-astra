import pytest

from jobpilot.state import (
    ALLOWED_TRANSITIONS,
    ApplicationState,
    InvalidTransition,
    can_transition,
    validate_transition,
)


def test_all_states_have_explicit_transition_rules():
    assert set(ALLOWED_TRANSITIONS) == {item.value for item in ApplicationState}
    assert all(set(targets) <= set(ALLOWED_TRANSITIONS) for targets in ALLOWED_TRANSITIONS.values())


def test_submitted_cannot_be_retried():
    assert not can_transition("SUBMITTED", "QUEUED")
    assert not can_transition("SUBMISSION_UNCONFIRMED", "SUBMITTING")
    assert can_transition("SUBMISSION_UNCONFIRMED", "SUBMITTED")
    with pytest.raises(InvalidTransition):
        validate_transition("SUBMITTED", "OPENING")


def test_captcha_handoff_and_resume_are_explicit():
    validate_transition("FILLING", "NEEDS_USER")
    validate_transition("NEEDS_USER", "FILLING")
    assert not can_transition("NONEXISTENT", "QUEUED")
