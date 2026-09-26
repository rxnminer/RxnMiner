# RxnMiner website

An English-only Astro site with the project overview, one visual workspace demo, and
ChemRxn-Bench comparisons. The visual palette follows the paper: blue `#7097C5` and
lavender `#B4AAD3`.

```bash
npm ci
npm run check
npm run build
npm run preview
```

The single visual demo is bundled under `public/media` and verified against
`demo-media.json` by `python scripts/audit_release.py` from the repository root. For project GitHub Pages, build with
`SITE_BASE=/RxnMiner`. From the repository root, run
`python3 scripts/verify_site.py` after that build to check local links and assets.

The benchmark covers 603 figures across 24 lanes.
Recompute the displayed aggregates with `python3 scripts/verify_benchmark.py` from
the repository root. Historical recognition results are clearly labeled.

See [deployment](../docs/website.md) for the workflow and hosting requirements.
