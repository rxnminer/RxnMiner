#!/usr/bin/env python3
"""Recompute website aggregates from the bundled anonymized figure metrics."""
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]


def verify():
    summary = json.loads((ROOT / 'website/public/data/benchmark.json').read_text())
    assert summary == json.loads((ROOT / 'website/src/data/benchmark.json').read_text())
    records = json.loads((ROOT / 'website/public/data/figure-metrics.json').read_text())
    figures = {r['figure_id'] for r in records}
    assert len(figures) == summary['figures'] == 603
    assert len({(r['figure_id'], r['lane']) for r in records}) == len(records)
    assert [c['id'] for c in summary['cohorts']] == ['full']
    for cohort in summary['cohorts']:
        expected_ids = figures
        lanes = defaultdict(list)
        for record in records:
            if record['figure_id'] in expected_ids:
                lanes[record['lane']].append(record)
        complete = {lane for lane, rows in lanes.items() if len(rows) == len(expected_ids)}
        assert len(cohort['rows']) == 23
        assert len({r['lane'] for r in cohort['rows']}) == 23
        assert {r['lane'] for r in cohort['rows'] if not r['pending']} == complete
        assert cohort['n'] == len(expected_ids)
        for row in cohort['rows']:
            if row['pending']:
                assert row['n'] == 0 and not lanes[row['lane']]
                assert all(v is None for mode in ('iso', 'ns') for v in row[mode].values())
                continue
            runs = lanes[row['lane']]
            assert {r['figure_id'] for r in runs} == expected_ids
            assert row['n'] == len(runs)
            for mode in ('iso', 'ns'):
                for target in ('reaction', 'molecule'):
                    values = {}
                    for metric in ('precision', 'recall'):
                        raw = [r[f'{target}_{metric}_{mode}'] for r in runs]
                        assert all(math.isfinite(v) and 0 <= v <= 1 for v in raw)
                        values[metric] = mean(raw)
                    pairs = [(r[f'{target}_precision_{mode}'], r[f'{target}_recall_{mode}'])
                             for r in runs]
                    values['f1'] = mean(2 * p * r / (p + r) if p + r else 0 for p, r in pairs)
                    for metric, value in values.items():
                        assert math.isclose(value, row[mode][f'{target}_{metric}'], abs_tol=1e-12)
        print(f"Verified {cohort['id']}: {cohort['n']} figures, {len(cohort['rows'])} methods")
    judge = json.loads((ROOT / 'website/public/data/judge.json').read_text())
    assert judge == json.loads((ROOT / 'website/src/data/judge.json').read_text())
    assert judge['figures'] == 603 and len(judge['rows']) == 23
    assert {r['lane'] for r in judge['rows']} == {r['lane'] for r in summary['cohorts'][0]['rows']}
    assert sum(not r['pending'] for r in judge['rows']) == 16
    for row in judge['rows']:
        assert row['n'] == (0 if row['pending'] else 603)
        for mode in ('iso', 'ns'):
            for metric in ('reaction_precision', 'reaction_recall', 'reaction_f1', 'molecule_f1'):
                value = row[mode][metric]
                if row['pending'] and metric != 'reaction_recall':
                    assert value is None
                else:
                    assert isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1
    print('Verified Judge snapshot: 16 judged lanes, 603 figures, both stereochemistry modes')


if __name__ == '__main__':
    verify()
