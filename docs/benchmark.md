# Benchmark snapshot and scoring

The bundled website snapshot was extracted from a validated public leaderboard archive.
It contains 603 figures, 8,510 normalized GT reactions, and five layout categories. The
task metadata records 8,547 tagged reactions; this is a different count from normalized GT.
The source archive SHA-256 is recorded in `website/public/data/benchmark.json`.

## Cohorts

- **Full set:** all 603 figures across the 23 published lanes.

The shared subset is not an estimate of the remaining 375 figures. Partial-coverage methods
are never ranked against complete-coverage methods using different image sets.

## Metrics

For each figure, compute reaction and molecule precision and recall. Average those values
over the cohort with equal figure weights. The displayed macro-F1 is the mean of
per-figure `2 * P * R / (P + R)`, with zero for a figure when both P and R are zero.
The Leaderboard and LLM Judge views use this same per-figure averaging rule,
separately for reaction/molecule metrics and strict/connectivity-only scoring.

Strict mode checks stereochemistry; connectivity-only mode ignores it. Failed and empty
answers remain in the denominator. The Qwen bare-Pi results use the first attempt only:
579 completed and 24 failed among 603 figures. Other lanes retain the validated archived
run-selection policy; this release does not silently reinterpret their attempts.

The Leaderboard snapshot comes from deterministic structure scoring. The LLM Judge
view uses judge precision and the original scorer's reaction recall to compute each
figure's reaction F1, and judge molecule precision/recall for molecule F1. It covers
16 judged lanes; the other seven lanes are not judged.
The original model predictions, corpus, source images, and private trajectories are not
bundled. Anonymized figure-level metrics are downloadable from the benchmark page so the
displayed aggregates can be recomputed with `python3 scripts/verify_benchmark.py`.

The archived runs are provided for reproducibility. A fresh evaluation is required before
treating these numbers as results of a different recognition deployment.
