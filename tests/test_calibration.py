"""Unit tests for per-publication reranker-score calibration (D-045).

These are pure-logic tests: no model server, no sockets, no device, no GPU. They exercise
the three surfaces of the feature that can be tested without the reranker:

  * pipeline.effective_top_score          -- the additive offset applied at the gate
  * tune_thresholds.would_answer          -- the same offset replayed in the offline sweep
  * compute_rerank_offsets.compute_offsets -- deriving offsets from score DISTRIBUTIONS

The single most important assertion in this file is the NO-OP proof: with the shipped
default (an empty offset map) the calibrated score is the raw score, so the gate behaves
byte-for-byte as it did before calibration existed. Everything else is guarding the claim
in D-045 that a per-publication offset lifts an unfairly-scored publication (TCCC scored
NEGATIVE at rank 1) over the threshold WITHOUT touching the publications that were fine.
"""
import sys
from pathlib import Path

import pytest

# conftest.py puts src/generation and src/retrieval on the path; the offset script lives
# under scripts/, which the deployed suite does not import, so add it explicitly here.
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from pipeline import effective_top_score  # noqa: E402
from tune_thresholds import would_answer  # noqa: E402
import compute_rerank_offsets as cro  # noqa: E402


# --------------------------------------------------------------------------------------
# NO-OP: empty offsets => effective score IS the raw score. The whole feature ships off.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [7.19, 3.71, 0.73, 0.0, -0.42, -5.91])
@pytest.mark.parametrize("pub", ["MCDP-1", "TCCC", "unknown-pub", None])
@pytest.mark.parametrize("offsets", [{}, None])
def test_empty_offsets_is_exact_noop(raw, pub, offsets):
    # With no calibration configured the raw score must be returned untouched -- the same
    # value the pre-D-045 gate compared to the threshold, for every publication.
    assert effective_top_score(raw, pub, offsets) == raw


def test_empty_offsets_preserves_none():
    # A None raw score (no chunk / no rerank) must stay None so the existing
    # `top_score is None` branch of the gate still fires.
    assert effective_top_score(None, "TCCC", {}) is None
    assert effective_top_score(None, "TCCC", None) is None


def test_would_answer_with_empty_offsets_matches_raw_gate():
    # The offline sweep with empty offsets must replay the ORIGINAL global-threshold gate.
    rec = {"top_score": 2.90, "top_pub_id": "TCCC", "model_declined": False,
           "n_claims": 0, "min_support": None}
    # 2.90 < 3.0 => refuse, exactly as the raw gate would, with {} and with default None.
    assert would_answer(rec, 3.0, None, {}) is False
    assert would_answer(rec, 3.0, None) is False
    rec_ok = dict(rec, top_score=3.10)
    assert would_answer(rec_ok, 3.0, None, {}) is True


# --------------------------------------------------------------------------------------
# A positive offset lifts exactly one publication over the gate; others are untouched.
# This is the D-045 fix: the rank-1-negative TCCC chunk should pass; MCDP is unaffected.
# --------------------------------------------------------------------------------------

def test_offset_lifts_only_target_publication_over_threshold():
    thr = 3.0
    offsets = {"TCCC": 4.0}   # derived, reviewed offset for the terse-clinical genre

    # TCCC scored NEGATIVE at rank 1 (D-045: inc-062, -0.42). Below the gate raw...
    assert effective_top_score(-0.42, "TCCC", {}) < thr
    # ...and above it once its genre offset is applied.
    assert effective_top_score(-0.42, "TCCC", offsets) >= thr

    # MCDP prose was already above the gate and has NO offset entry: untouched.
    assert effective_top_score(5.00, "MCDP-1", offsets) == 5.00
    assert effective_top_score(5.00, "MCDP-1", offsets) >= thr

    # A publication with no entry that was genuinely below the gate STAYS below -- the
    # offset does not blanket-loosen the threshold, it only re-centres named genres.
    assert effective_top_score(1.00, "MCWP-3", offsets) == 1.00
    assert effective_top_score(1.00, "MCWP-3", offsets) < thr


def test_offset_changes_gate_decision_in_sweep():
    # Same lift, but exercised through the offline sweep's gate replay.
    rec_tccc = {"top_score": -0.42, "top_pub_id": "TCCC", "model_declined": False,
                "n_claims": 0, "min_support": None}
    rec_mcdp = {"top_score": 5.00, "top_pub_id": "MCDP-1", "model_declined": False,
                "n_claims": 0, "min_support": None}
    offsets = {"TCCC": 4.0}

    assert would_answer(rec_tccc, 3.0, None, {}) is False        # refused raw
    assert would_answer(rec_tccc, 3.0, None, offsets) is True     # answered calibrated
    assert would_answer(rec_mcdp, 3.0, None, offsets) is True     # untouched, still answers


def test_missing_publication_gets_zero_offset():
    # A publication absent from the map contributes +0.0, never a guessed value.
    offsets = {"TCCC": 4.0}
    assert effective_top_score(2.0, "not-in-map", offsets) == 2.0
    assert effective_top_score(2.0, None, offsets) == 2.0


# --------------------------------------------------------------------------------------
# compute_offsets aligns per-publication score DISTRIBUTIONS, not eval pass/fail.
# --------------------------------------------------------------------------------------

def _rec(pub, score, kind="in"):
    return {"top_pub_id": pub, "top_score": score, "kind": kind,
            "model_declined": False, "n_claims": 0, "min_support": None}


def test_compute_offsets_aligns_medians_to_reference():
    # Three publications with clearly different score LOCATIONS and identical spread.
    #   A median 5, B median 0, C median 2  ->  median-of-medians reference = 2 (pub C).
    records = (
        [_rec("A", s) for s in (4.0, 5.0, 6.0)] +
        [_rec("B", s) for s in (-1.0, 0.0, 1.0)] +
        [_rec("C", s) for s in (1.0, 2.0, 3.0)]
    )
    offsets, report = cro.compute_offsets(records, percentile=50, reference="median",
                                          min_samples=3)

    # offset_p = reference - median_p ; reference is the middle publication's median (2).
    assert offsets == {"A": -3.0, "B": 2.0, "C": 0.0}

    # After applying the additive offset every publication's median lands on the reference:
    # that IS the alignment claim, and it holds regardless of which questions passed.
    for pub in ("A", "B", "C"):
        assert report["pubs"][pub]["median_after"] == pytest.approx(report["reference_value"])
    assert report["reference_value"] == 2.0


def test_compute_offsets_skips_small_samples_as_noise():
    # A publication with fewer than min_samples rank-1 scores earns NO offset -- estimating
    # a shift from one or two points is the noise-fitting D-044 warns against.
    records = (
        [_rec("A", s) for s in (4.0, 5.0, 6.0)] +
        [_rec("B", s) for s in (-1.0, 0.0, 1.0)] +
        [_rec("TINY", 9.9), _rec("TINY", -9.9)]      # only 2 samples
    )
    offsets, report = cro.compute_offsets(records, min_samples=3)
    assert "TINY" not in offsets
    assert report["skipped"].get("TINY") == 2


def test_compute_offsets_ignores_out_of_corpus_by_default():
    # Out-of-corpus rank-1 chunks are spurious matches; by default they do not enter the
    # genre-scale estimate. This uses the in/out label to pick the POPULATION only -- never
    # a per-question pass/fail outcome.
    records = (
        [_rec("A", s) for s in (4.0, 5.0, 6.0)] +
        [_rec("B", s) for s in (-1.0, 0.0, 1.0)] +
        [_rec("A", 100.0, kind="out"), _rec("B", 100.0, kind="out")]
    )
    offsets, _ = cro.compute_offsets(records, min_samples=3)
    # The out-of-corpus 100.0 spikes are excluded, so medians are still 5 and 0.
    assert offsets == {"A": -2.5, "B": 2.5}   # reference = mean-free median of {5,0} = 2.5


def test_compute_offsets_empty_input_is_safe():
    # No usable records => no offsets and no reference; the operator sees an explicit no-op.
    offsets, report = cro.compute_offsets([], min_samples=3)
    assert offsets == {}
    assert report["reference_value"] is None


# --------------------------------------------------------------------------------------
# The shipped config must be the no-op: an empty offset map.
# --------------------------------------------------------------------------------------

def test_shipped_config_offsets_are_valid():
    # Was "== {}" when the mechanism first shipped as a no-op (agent B). Real per-pub
    # offsets were applied 25 Aug from the 999-item probe (D-054), so the assertion now
    # is that the shipped offsets are well-formed: a map of pub_id -> float, with TCCC
    # (the genre outlier D-045 found) carrying a positive lift. The empty-map no-op path
    # is still proven by the other tests in this file.
    yaml = pytest.importorskip("yaml")
    cfg = yaml.safe_load((REPO / "config" / "default.yaml").read_text())
    offs = cfg["abstention"]["reranker_score_offsets"]
    assert isinstance(offs, dict) and offs, "expected applied offsets"
    assert all(isinstance(v, (int, float)) for v in offs.values())
    assert offs.get("TCCC", 0) > 0, "TCCC is the low-scoring genre outlier; expect a positive lift"
