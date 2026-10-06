import datetime as dt

import pytest

from applypilot.gradwindow import check

C = dt.date(2028, 5, 15)  # candidate expected graduation

# Real sentences from Summer 2027 postings (Oct 2026), with the expected outcome for a May 2028 graduate.
CASES = [
    ("• Acceptable Graduation Dates: December 2027 - June 2028", True),
    ("Expected graduation between December 2027 – June 2029", True),
    ("Currently pursuing a bachelor's degree in Marketing, graduating in 2028 or 2029.", True),
    ("You have an expected graduation date between December 2027 and June 2029", True),
    ("Currently enrolled in a full-time, graduate degree program with an expected graduation date in Winter 2027 or Spring 2028.", True),
    ("Must have an expected graduation date no later than June 2029.", True),
    ("Expected graduation date of May 2028.", True),
    ("Are you a high school student graduating in June 2027? We have an exciting internship", None),  # no mandatory wording → Astra
    ("Currently enrolled in a bachelor's degree program and be a Junior graduating by summer of 2028", True),
    ("Must have an expected graduation date of December 2027 - June 2028", True),
    ("Must be a current, matriculated student in an accredited program graduating December 2027 or later", True),
    ("with a graduation date no earlier than April 2027", True),
    ("Graduating in Fall 2027 or Spring 2028 with a B.S., M.S. or Ph.D. in Computer Science", True),
    ("Graduating in Dec 2027 or May 2028 (U.S.)", True),
    ("You're an undergraduate college student enrolled in an accredited program, not graduating before December 2027 .", True),
    ("On track to graduate by June 2029", True),
    ("set to graduate in the spring/summer of 2028, ideally studying economics", None),  # soft wording → Astra
    ("Graduating in the 2028 calendar year", True),
    ("Must be graduating between December 2027 and August 2029 and have at least one additional quarter", True),
    ("Must be graduating between September 2027 – July 2028", True),
    ("Targeting graduation in Summer 2028 or earlier.", True),
    ("You will graduate between fall 2027 and Summer 2028 with a degree in Computer Science", True),
    ("slated to graduate no earlier than December 2027.", True),
    ("Must be graduating between December 2026 and June 2027", False),
    ("Class of 2030 students only", False),
    # Regressions from live data:
    ("Must be graduating between December 2027 and June 2028 and have at least one additional quarter of school "
     "remaining following the completion of the Summer 2027 internship.", True),
    ("Expected graduation date no later then December 2028 .", True),
    ("Students graduating in Fall 2027, or graduating in Spring 2027 and interested in a full-time role", None),
    ("Pursuing a degree with a graduation date of December 2028 or later", None),
    ("Must be pursuing a degree with a graduation date of December 2028 or later", False),
    ("Must graduate by December 2027", False),
]


@pytest.mark.parametrize("sentence,expected", CASES)
def test_real_graduation_sentences(sentence, expected):
    decision, notes = check(sentence, C)
    assert decision is expected, notes


def test_unknown_candidate_date_defers():
    assert check("Graduating in 2028", None)[0] is None


def test_mixed_windows_defer_to_astra():
    text = "Graduating in 2028.\nMust graduate by December 2027."
    assert check(text, C)[0] is None
