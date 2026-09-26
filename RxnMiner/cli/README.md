# RxnMiner command line

Run from the repository root on Linux, macOS, or WSL2. See [getting started](../../docs/getting-started.md).

```bash
npm ci
node ./rxnminer.mjs setup
node ./rxnminer.mjs doctor --json
node ./rxnminer.mjs demo demo-figure
```

The sealed replay opens the image workbench without model calls. For a new extraction, configure
UniParser credentials and a vision-capable model, then upload a PNG, JPEG, WebP, or TIFF image
in the workbench or run:

```bash
node ./rxnminer.mjs login
export UNIPARSER_API_KEY=your-key
node ./rxnminer.mjs extract figure.png --pi-auth
node ./rxnminer.mjs sessions
node ./rxnminer.mjs result SESSION_ID
node ./rxnminer.mjs export SESSION_ID --format v2 --output reactions.json
```

For your own model API key, set `CHEMRXN_MODEL_API_KEY` and pass `--provider` and `--model`.
`--thinking`, `--message`, and `--no-browser` control the extraction request and presentation.

```bash
node ./rxnminer.mjs review prediction.v2.json --image figure.png --perception molparser.json
node ./rxnminer.mjs start
node ./rxnminer.mjs status
node ./rxnminer.mjs stop
```

The Reviewer displays the source image, detection boxes, molecular structures, and reaction records.
Ketcher corrections are saved separately from immutable extraction output. Exports include V2 JSON,
CSV, RXN, reports, attribution, and reviewed results. `chemrxn` remains a compatibility alias.

PDF and supporting-information extraction are not included.
