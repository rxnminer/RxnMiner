# RxnMiner workbench

The local application is contained here. Run the supported commands from the repository root:

```bash
npm ci
node ./rxnminer.mjs setup
node ./rxnminer.mjs demo demo-figure --no-browser
```

Use Linux, macOS, or WSL2 on Windows. Root npm scripts delegate to this directory.
When following component documentation, run commands from `RxnMiner/` unless stated otherwise.

| Component | Responsibility |
|---|---|
| `cli/` | Command parsing, setup, health checks, extraction, and export |
| `runtime/pi-service/` | Pi sessions, replay, immutable events, and artifact contracts |
| `reviewer/` | Source-linked visual review, structure editing, and versioned corrections |
| `harness/` | Local HTTP workspace, input handling, and figure processing |
| `figure_skills/` | Figure extraction skills and chemistry tools |
| `tests/`, component test folders | Recognition, contract, integration, and UI checks |
| `packaging/`, `scripts/` | Private-package integrity and agent integration |

The supported input is one reaction image (PNG, JPEG, WebP, or TIFF). Figure samples and replay metadata are retained for tests.
Predictions and source hashes remain immutable; evaluation truth never enters extraction prompts.

See [setup](../docs/getting-started.md), [licensing](../docs/LICENSING.md), and
[third-party notices](../docs/THIRD_PARTY_NOTICES.md).
