#!/usr/bin/env python3
"""Import the completed 1017 evaluation without publishing private run metadata."""
import argparse
import hashlib
import json
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    load = lambda path: json.loads(path.read_text(encoding='utf-8'))
    report = load(args.archive / 'summary.json')
    usage = load(args.archive / 'official-efficiency.json')
    raw = [json.loads(line) for line in (args.archive / 'per_figure.jsonl').read_text(encoding='utf-8').splitlines()]
    assert report['complete'] and report['readiness']['direct_terminal'] == report['readiness']['rxnminer_terminal'] == 603
    assert report['direct_max_tokens'] == 384000
    summary = load(ROOT / 'website/src/data/benchmark.json')
    judge = load(ROOT / 'website/src/data/judge.json')
    records = load(ROOT / '.local/benchmark/figure-metrics.json')
    ids = {r['figure_id'] for r in records}
    names = {'direct': 'DeepSeek V4.1 Flash 1017',
             'rxnminer': 'RxnMiner · DeepSeek V4.1 Flash 1017'}
    for lane, name in names.items():
        lane_id = 'deepseek-v4.1-flash-1017-' + lane
        rows = [r for r in raw if r['lane'] == lane and r['mode'] == 'raw']
        assert len(rows) == 603 and not any(r['score_error'] for r in rows)
        hashed = [hashlib.sha256(r['task'].encode()).hexdigest()[:16] for r in rows]
        assert len(set(hashed)) == 603 and set(hashed) == ids
        records = [r for r in records if r['lane'] != lane_id]
        for r, figure_id in zip(rows, hashed):
            record = {'figure_id': figure_id, 'lane': lane_id}
            for mode in ('iso', 'ns'):
                for metric in ('precision', 'recall', 'f1'):
                    record[f'reaction_{metric}_{mode}'] = r[f'reaction_{metric}_{mode}']
                # The pinned scorer export contains molecule F1, but not molecule P/R.
                # Preserve the measured F1; do not invent the unavailable components.
                record[f'molecule_precision_{mode}'] = None
                record[f'molecule_recall_{mode}'] = None
                record[f'molecule_f1_{mode}'] = r[f'molecule_f1_{mode}']
            records.append(record)
        urows = [r for r in usage['rows'] if r['lane'] == lane]
        assert len(urows) == 603 and {r['task'] for r in urows} == {r['task'] for r in rows}
        row = dict(lane=lane_id, name=name, group='Direct' if lane == 'direct' else 'RxnMiner', n=603, pending=False)
        for mode in ('iso', 'ns'):
            row[mode] = {f'reaction_{m}': mean(r[f'reaction_{m}_{mode}'] for r in rows) for m in ('precision', 'recall', 'f1')}
            row[mode].update(molecule_precision=None, molecule_recall=None,
                             molecule_f1=mean(r[f'molecule_f1_{mode}'] for r in rows))
        row.update(avg_time_s=mean(r['seconds'] for r in urows),
                   avg_cost_usd=mean(r['scheduled_usd'] for r in urows),
                   avg_tokens=mean(r['total'] for r in urows),
                   cost_is_lower_bound=True, tokens_are_lower_bound=True,
                   usage_missing_pages=sum(r['missing_usage_calls'] > 0 for r in urows),
                   usage_missing_calls=sum(r['missing_usage_calls'] for r in urows))
        summary['cohorts'][0]['rows'] = [r for r in summary['cohorts'][0]['rows'] if r['lane'] != lane_id] + [row]
        jrow = dict(lane=lane_id, name=name, group=row['group'], n=0, pending=True, coverage='0/603', judge_overall=None)
        for mode in ('iso', 'ns'):
            jrow[mode] = dict(reaction_precision=None, reaction_recall=row[mode]['reaction_recall'], reaction_f1=None, molecule_f1=None)
        judge['rows'] = [r for r in judge['rows'] if r['lane'] != lane_id] + [jrow]
    # Earlier extraction and Judge runs remain individually identifiable.
    for dataset in (summary['cohorts'][0]['rows'], judge['rows']):
        for row in dataset:
            if row['lane'] in ('deepseek-v4.1-flash-direct-think', 'deepseek-v4.1-flash-rxnminer'):
                row['name'] = row['name'].replace(' (historical)', '') + ' (historical)'
    summary['cohorts'][0]['rows'].sort(key=lambda r: (r['pending'], -(r['iso']['reaction_f1'] or 0)))
    summary['updates'] = [u for u in summary.get('updates', []) if u.get('id') != 'deepseek-1017'] + [dict(
        id='deepseek-1017', evaluation_date='2026-11-10', figures=603, rdkit=report['rdkit'],
        scorer_sha256=report['scorer_sha256'],
        source_sha256={f: hashlib.sha256((args.archive / f).read_bytes()).hexdigest()
                       for f in ('summary.json', 'per_figure.jsonl', 'official-efficiency.json')},
        method='Pinned scorer per-figure scores, macro-averaged over all 603 figures. Failed/empty answers score zero; usable partial RxnMiner answers are retained. Molecule P/R are unavailable in this export.',
        cost_basis='Recorded requests priced using the 2026-11-10 official peak/off-peak schedule at request start. Lower bounds; missing usage is unknown. Latest retained execution only; archived retries excluded.',
        pricing_url=usage['pricing_url'], rates_usd_per_million=usage['rates_usd_per_million'])]
    judge['lanes'] = len(judge['rows'])
    for name, data in [('benchmark', summary), ('judge', judge)]:
        for area in ('src', 'public'):
            (ROOT / f'website/{area}/data/{name}.json').write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')
    (ROOT / '.local/benchmark').mkdir(parents=True, exist_ok=True)
    (ROOT / '.local/benchmark/figure-metrics.json').write_text(json.dumps(records, separators=(',', ':'))+'\n', encoding='utf-8', newline='\n')
    print('Imported two 603-figure 1017 lanes; retained historical lanes and left new Judge scores unavailable.')


if __name__ == '__main__':
    main()
