from datetime import UTC, datetime

from app.config import Settings
from app.domain import IdeaHorizon
from app.horizons import get_horizon_profile
from app.research_eval import (
    calibration_candidates,
    chronological_splits,
    walk_forward_splits,
)


def test_chronological_splits_are_ordered_and_test_is_never_training_data() -> None:
    splits = chronological_splits(
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2025, 1, 1, tzinfo=UTC),
        train_fraction=0.6,
        validation_fraction=0.2,
        test_fraction=0.2,
    )

    assert splits.train.end == splits.validation.start
    assert splits.validation.end == splits.test.start
    assert splits.train.start < splits.train.end < splits.validation.end < splits.test.end


def test_walk_forward_uses_expanding_past_and_next_unseen_window() -> None:
    folds = walk_forward_splits(
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2025, 1, 1, tzinfo=UTC),
        folds=3,
    )

    assert len(folds) == 3
    assert all(train.end == test.start for train, test in folds)
    assert all(train.start == folds[0][0].start for train, _ in folds)
    assert folds[0][1].end == folds[1][1].start


def test_declared_calibration_grid_is_small_and_does_not_mutate_production_defaults() -> None:
    candidates = calibration_candidates()
    base_settings = Settings(_env_file=None)
    base_profile = get_horizon_profile(IdeaHorizon.SWING_5D)

    assert 2 <= len(candidates) <= 10
    assert {candidate.scoring_model for candidate in candidates} == {"legacy", "weighted"}
    research_settings = candidates[-1].settings(base_settings)
    research_profile = candidates[-1].profile(base_profile)
    assert base_settings.technical_scoring_model == "legacy"
    assert get_horizon_profile(IdeaHorizon.SWING_5D) == base_profile
    assert research_settings.technical_scoring_model == "weighted"
    assert research_profile.timeframe_weights != base_profile.timeframe_weights
