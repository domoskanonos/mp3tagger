"""Tests for :mod:`radio_ripper.services.collection_manager`."""

from __future__ import annotations

from radio_ripper.infra.catalog import SongRecord
from radio_ripper.services.collection_manager import (
    is_better_version,
    is_same_version,
    pick_eviction_candidate,
)

# ── is_same_version ────────────────────────────────────────────────────────


class TestIsSameVersion:
    def test_same_mbid_same_isrc_true(self):
        assert is_same_version("mb1", "ISRC1", "mb1", "ISRC1")

    def test_different_mbid_false(self):
        assert not is_same_version("mb1", "ISRC1", "mb2", "ISRC1")

    def test_new_without_mbid_false(self):
        assert not is_same_version(None, "ISRC1", "mb1", "ISRC1")

    def test_old_without_mbid_false(self):
        assert not is_same_version("mb1", "ISRC1", None, "ISRC1")

    def test_new_without_isrc_false(self):
        assert not is_same_version("mb1", None, "mb1", "ISRC1")

    def test_old_without_isrc_false(self):
        assert not is_same_version("mb1", "ISRC1", "mb1", None)

    def test_both_without_isrc_false(self):
        assert not is_same_version("mb1", None, "mb1", None)

    def test_same_mbid_different_isrc_false(self):
        assert not is_same_version("mb1", "ISRC1", "mb1", "ISRC2")


# ── is_better_version ───────────────────────────────────────────────────────


class TestIsBetterVersion:
    def test_higher_score_wins(self):
        assert is_better_version(0.95, 192, 44100, 0.85, 320, 44100)

    def test_lower_score_loses_even_with_higher_bitrate(self):
        assert not is_better_version(0.80, 320, 44100, 0.95, 128, 44100)

    def test_equal_score_higher_bitrate_wins(self):
        assert is_better_version(0.90, 320, 44100, 0.90, 192, 44100)

    def test_equal_score_equal_bitrate_higher_samplerate_wins(self):
        assert is_better_version(0.90, 320, 48000, 0.90, 320, 44100)

    def test_equal_everything_returns_false(self):
        assert not is_better_version(0.90, 320, 44100, 0.90, 320, 44100)

    def test_none_score_treated_as_zero(self):
        assert is_better_version(0.10, 128, 44100, None, 320, 44100)

    def test_none_bitrate_treated_as_zero(self):
        assert is_better_version(0.90, None, 44100, 0.90, None, 44100) is False
        assert is_better_version(0.90, 64, 44100, 0.90, None, 44100)


# ── pick_eviction_candidate ─────────────────────────────────────────────────


def _rec(file_path: str, rank: int | None = None) -> SongRecord:
    return SongRecord(file_path=file_path, popularity_rank=rank)


class TestPickEvictionCandidate:
    def test_empty_candidates_returns_none(self):
        assert pick_eviction_candidate([], 50000) is None

    def test_new_rank_none_returns_none(self):
        assert pick_eviction_candidate([_rec("/a.mp3", 100)], None) is None

    def test_all_candidates_rank_null_returns_none(self):
        assert pick_eviction_candidate([_rec("/a.mp3", None), _rec("/b.mp3", None)], 50000) is None

    def test_all_ranks_ge_new_rank_returns_none(self):
        cands = [_rec("/a.mp3", 50000), _rec("/b.mp3", 99999)]
        assert pick_eviction_candidate(cands, 50000) is None

    def test_returns_lowest_rank_below_threshold(self):
        cands = [_rec("/a.mp3", 50000), _rec("/b.mp3", 10000), _rec("/c.mp3", 99999)]
        result = pick_eviction_candidate(cands, 30000)
        assert result is not None
        assert result.popularity_rank == 10000

    def test_returns_none_when_exact_equal_rank(self):
        cands = [_rec("/a.mp3", 50000)]
        assert pick_eviction_candidate(cands, 50000) is None

    def test_sorts_unranked_to_end(self):
        cands = [_rec("/a.mp3", None), _rec("/b.mp3", 10000)]
        result = pick_eviction_candidate(cands, 30000)
        assert result is not None
        assert result.popularity_rank == 10000

    def test_returns_lowest_rank_when_multiple_below(self):
        cands = [_rec("/a.mp3", 5000), _rec("/b.mp3", 1000), _rec("/c.mp3", 9000)]
        result = pick_eviction_candidate(cands, 10000)
        assert result is not None
        assert result.popularity_rank == 1000
