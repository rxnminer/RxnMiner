#!/usr/bin/env python3
"""Import aggregate scores and anonymized metrics from a benchmark HTML snapshot."""
import argparse
import hashlib
import json
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
KEYS = [f'{target}_{metric}_{mode}' for mode in ('iso', 'ns')
        for target in ('reaction', 'molecule') for metric in ('precision', 'recall')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    args = parser.parse_args()
    raw = args.snapshot.read_bytes()
    html = raw.decode('utf8')
    data, _ = json.JSONDecoder().raw_decode(html.split('const D=', 1)[1])
    tasks, variants = data['tasks'], data['expected_variants']
    assert len({t['id'] for t in tasks}) == len(tasks)
    variants = [v for v in variants if v['lane'] != 'gemini-rxnminer']
    assert len({v['lane'] for v in variants}) == len(variants) == 23
    records, rows = [], []
    for variant in variants:
        lane = variant['lane']
        runs = []
        for task in tasks:
            matches = [r for r in task['runs'] if r['lane'] == lane]
            assert len(matches) <= 1, f'Duplicate run: {lane}'
            if not matches:
                continue
            run = matches[0]
            metrics = {key: run.get('dmetrics', {}).get(key, run.get(key)) for key in KEYS}
            assert all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in metrics.values())
            record = dict(figure_id=hashlib.sha256(task['id'].encode()).hexdigest()[:16],
                          lane=lane, **metrics)
            records.append(record)
            runs.append(record)
        assert len(runs) in (0, len(tasks)), f'Incomplete lane: {lane}'
        group = {'direct': 'Direct', 'rxnminer': 'RxnMiner', 'baseline': 'Baseline'}[variant['phase']]
        name = variant['model']
        row = dict(lane=lane, name=f'RxnMiner · {name}' if group == 'RxnMiner' else name,
                   group=group, n=len(runs), pending=not runs)
        for mode in ('iso', 'ns'):
            scores = {}
            for target in ('reaction', 'molecule'):
                p, r = [mean(run[f'{target}_{metric}_{mode}'] for run in runs) if runs else None
                        for metric in ('precision', 'recall')]
                per_figure = [(run[f'{target}_precision_{mode}'], run[f'{target}_recall_{mode}'])
                              for run in runs]
                scores.update({f'{target}_precision': p, f'{target}_recall': r,
                               f'{target}_f1': mean(2*p*r/(p+r) if p+r else 0
                                                   for p, r in per_figure) if runs else None})
            row[mode] = scores
        rows.append(row)
    rows.sort(key=lambda r: (r['pending'], -(r['iso']['reaction_f1'] or 0)))
    summary = dict(snapshot_date='public', figures=len(tasks),
                   gt_reactions=sum(t['n_gt'] for t in tasks), layouts=5,
                   source_sha256=hashlib.sha256(raw).hexdigest(),
                   method='Precision, recall, and F1 are computed per figure, then averaged with equal figure weights (macro-F1). Missing individual outputs score as empty predictions. Pending lanes are not ranked.',
                   cohorts=[dict(id='full', n=len(tasks), label='Full benchmark', rows=rows)])
    for target in ('website/src/data/benchmark.json', 'website/public/data/benchmark.json'):
        (ROOT / target).write_text(json.dumps(summary, indent=2)+'\n', encoding='utf8', newline='\n')
    (ROOT / 'website/public/data/figure-metrics.json').write_text(
        json.dumps(records, separators=(',', ':'))+'\n', encoding='utf8', newline='\n')
    print(f"Imported {len(tasks)} figures and {len(rows)} lanes ({sum(r['pending'] for r in rows)} pending)")


if __name__ == '__main__':
    main()
