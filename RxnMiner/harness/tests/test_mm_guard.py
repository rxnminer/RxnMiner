"""MarkushMapMulti safety guards (tools_product/uniparser_to_v2.reverse_r).

MM's RMappingResult.from_mapping runs an unbounded-recursion MCS clique search that
stack-overflows / RDKit-C-segfaults on dense fused/bridged cages, killing the whole
process when run over a large batch. reverse_r now guards it with a heavy-atom SIZE GATE
(skip pathological inputs before calling MM) + a SIGALRM TIMEOUT (abort a hung recursion).
These tests pin both: oversized inputs are skipped fast, normal inputs still reverse."""
import os
import time

import pytest

uni = pytest.importorskip("tools_product.uniparser_to_v2")


def test_size_gate_skips_oversized_fast():
    """A concrete product above MM_MAX_HEAVY must return {} immediately (never reach MM)."""
    big = "C" * (uni._MM_MAX_HEAVY + 20)          # well over the heavy-atom cap
    assert uni._heavy_atoms(big) > uni._MM_MAX_HEAVY
    t0 = time.time()
    out = uni.reverse_r("*CCC<sep><a>0:R1</a>", big)
    assert out == {}                              # skipped, not attempted
    assert time.time() - t0 < 1.0                 # and skipped FAST (no MCS blowup)


def test_normal_reverse_still_works():
    """A small markush product vs a concrete product still reverses to an R fragment."""
    if not uni._HAVE_MM:
        pytest.skip("MarkushMapMulti not importable in this env")
    out = uni.reverse_r("*c1ccccc1<sep><a>0:R</a>", "Clc1ccccc1")
    # MM (or its guard) must still yield the chloro R-group, not crash or hang
    assert isinstance(out, dict)
    assert any("Cl" in (v or "") for v in out.values())


def test_size_gate_decides_whether_mm_is_called(monkeypatch):
    """Pin the GATE itself by spying on the MM entry point (RMappingResult.from_mapping): over the cap it must
    NOT be reached, under the cap it must be. Asserting on reverse_r's RETURN value cannot test this -- where
    MarkushMapMulti is absent (_HAVE_MM False) reverse_r returns {} either way, so the test would be vacuous
    in one env and fail in another. Spying works whether or not MM is installed."""
    calls = []

    class _SpyResult:
        r_groups = []

        @staticmethod
        def from_mapping(template, concrete):
            calls.append(concrete)
            return _SpyResult()

    monkeypatch.setattr(uni, "_HAVE_MM", True)                  # take the MM branch regardless of the env
    monkeypatch.setattr(uni, "RMappingResult", _SpyResult, raising=False)
    # the RDKit fast path returns first when it succeeds -> disable it so the gate is what decides
    monkeypatch.setattr(uni, "_heavy_atoms", uni._heavy_atoms)  # keep the real sizer
    tpl = "*c1ccccc1<sep><a>0:R</a>"

    monkeypatch.setattr(uni, "_MM_MAX_HEAVY", 8)
    uni.reverse_r(tpl, "C" * 400)                                # 400 heavy atoms, way over the cap
    assert calls == [], "size gate must return before ever calling MM on an oversize input"

    monkeypatch.setattr(uni, "_MM_MAX_HEAVY", 10_000)            # cap lifted
    uni.reverse_r(tpl, "C" * 400)
    assert len(calls) == 1, "under the cap the MM path must actually be entered"
