"""Grading rules for the public track record — these encode 'no cherry-picking'."""

from scripts.track_record import grade_exit_alarm, grade_prewarn

T0 = 1_000_000


def test_exit_alarm_correct_when_price_lower_after():
    pts = [(T0, 10.0), (T0 + 1800, 4.0), (T0 + 3500, 2.0), (T0 + 3700, 2.0)]
    g = grade_exit_alarm(T0, None, None, pts)
    assert g["outcome"] == "correct"
    assert g["exit_saved_pct"] == 80.0        # last print inside the hour = 2.0


def test_exit_alarm_wrong_when_price_higher_stays_on_the_record():
    pts = [(T0, 10.0), (T0 + 3000, 15.0), (T0 + 3700, 15.0)]
    g = grade_exit_alarm(T0, None, None, pts)
    assert g["outcome"] == "wrong"           # a bad call is graded, never dropped
    assert g["exit_saved_pct"] == -50.0


def test_exit_alarm_pending_until_tape_covers_the_hour():
    pts = [(T0, 10.0), (T0 + 1200, 5.0)]     # tape ends 20min after alert
    assert grade_exit_alarm(T0, None, None, pts)["outcome"] == "pending"


def test_prewarn_graded_against_its_own_claim():
    assert grade_prewarn(T0, exit_offset_s=300, tape_span_s=4000)["outcome"] == "correct"
    assert grade_prewarn(T0, exit_offset_s=900, tape_span_s=4000)["outcome"] == "wrong"
    # long tape with NO exit observed = the warning was wrong, not "unknown"
    assert grade_prewarn(T0, exit_offset_s=None, tape_span_s=4000)["outcome"] == "wrong"
    # short tape = genuinely can't grade yet
    assert grade_prewarn(T0, exit_offset_s=None, tape_span_s=800)["outcome"] == "ungradable"
