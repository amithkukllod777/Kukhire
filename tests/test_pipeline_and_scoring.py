import pytest

from app.pipeline import LEGACY_STAGE_MAP, OPEN_STAGES, STAGES, TransitionError, validate_transition
from app.scoring import CriteriaError, validate_criteria, weighted_overall


class TestPipeline:
    def test_forward_skip_and_back_between_open_stages(self):
        for a in OPEN_STAGES:
            for b in OPEN_STAGES:
                if a != b:
                    assert validate_transition(a, b).to_stage == b

    def test_hired_only_from_offer(self):
        assert validate_transition("offer", "hired").to_stage == "hired"
        for stage in ("applied", "screening", "interview"):
            with pytest.raises(TransitionError, match="offer"):
                validate_transition(stage, "hired")

    @pytest.mark.parametrize("exit_stage", ["rejected", "withdrawn"])
    def test_any_open_stage_can_exit(self, exit_stage):
        for stage in OPEN_STAGES:
            assert validate_transition(stage, exit_stage).to_stage == exit_stage

    def test_hired_is_final(self):
        for target in STAGES:
            if target != "hired":
                with pytest.raises(TransitionError, match="HRMS"):
                    validate_transition("hired", target, reason="because")

    @pytest.mark.parametrize("closed", ["rejected", "withdrawn"])
    def test_reopen_needs_a_reason_and_goes_to_applied(self, closed):
        with pytest.raises(TransitionError, match="reason"):
            validate_transition(closed, "applied")
        assert validate_transition(closed, "applied", reason="Reconsidered").reopened is True
        with pytest.raises(TransitionError, match="reopened"):
            validate_transition(closed, "interview", reason="x")

    def test_same_stage_and_unknown_stage_are_refused(self):
        with pytest.raises(TransitionError, match="already"):
            validate_transition("applied", "applied")
        with pytest.raises(TransitionError, match="not a pipeline stage"):
            validate_transition("applied", "teleported")
        with pytest.raises(TransitionError, match="unknown stage"):
            validate_transition("mystery", "applied")

    def test_legacy_stage_map_targets_real_stages_and_does_not_guess(self):
        assert set(LEGACY_STAGE_MAP.values()) <= set(STAGES)
        assert "on_hold" not in LEGACY_STAGE_MAP and "shortlisted" not in LEGACY_STAGE_MAP


GOOD = [
    {"key": "python", "label": "Python", "weight": 60, "description": "x"},
    {"key": "comms", "label": "Comms", "weight": 40},
]


class TestScoring:
    def test_valid_criteria_are_normalized(self):
        out = validate_criteria(GOOD)
        assert out[1] == {"key": "comms", "label": "Comms", "weight": 40.0, "description": ""}

    @pytest.mark.parametrize(
        "bad,match",
        [
            ("nope", "list"),
            ([], "between"),
            ([{"key": f"k{i}", "label": "l", "weight": 1} for i in range(11)], "between"),
            ([{"key": "Bad Key", "label": "l", "weight": 1}], "key"),
            ([{"key": "a", "label": "", "weight": 1}], "label"),
            ([{"key": "a", "label": "l", "weight": 0}], "weight"),
            ([{"key": "a", "label": "l", "weight": 101}], "weight"),
            ([{"key": "a", "label": "l", "weight": True}], "weight"),
            ([{"key": "a", "label": "l", "weight": 1, "extra": 1}], "unknown"),
            ([{"key": "a", "label": "l", "weight": 1}, {"key": "a", "label": "m", "weight": 1}], "twice"),
            (["x"], "object"),
        ],
    )
    def test_invalid_criteria_are_rejected(self, bad, match):
        with pytest.raises(CriteriaError, match=match):
            validate_criteria(bad)

    def test_weighted_overall_is_computed_from_scores(self):
        criteria = validate_criteria(GOOD)
        assert weighted_overall(criteria, {"python": 10, "comms": 0}) == 6.0
        assert weighted_overall(criteria, {"python": 5, "comms": 5}) == 5.0

    def test_missing_or_out_of_range_scores_are_errors_not_zeros(self):
        criteria = validate_criteria(GOOD)
        with pytest.raises(CriteriaError, match="no score"):
            weighted_overall(criteria, {"python": 5})
        with pytest.raises(CriteriaError, match="outside"):
            weighted_overall(criteria, {"python": 11, "comms": 5})
