# Getting started

Use Linux, macOS, or WSL2 with Node.js 22.19+, Python 3.11, Java 8+, Git, and Bash.
The workbench installs its Python environment in your local user data directory.

```bash
npm ci
node ./rxnminer.mjs setup
node ./rxnminer.mjs doctor --json
node ./rxnminer.mjs demo demo-figure --no-browser
```

Open the printed `workspaceUrl`. The demo is a recorded replay: it does not submit a new
model or molecule-recognition request.

For live work, run `node ./rxnminer.mjs login`, select your model account, and configure
[UniParser](uniparser.md). You can then run:

```bash
node ./rxnminer.mjs extract figure.png --pi-auth
node ./rxnminer.mjs sessions
node ./rxnminer.mjs result SESSION_ID
node ./rxnminer.mjs export SESSION_ID --format v2
```

The workbench retains source evidence and original predictions. Ketcher edits are staged
for review and saved as a separate reviewed version when applied.

`chemrxn` remains a compatibility command for existing scripts; `rxnminer` is the submission
edition's entry point. Internal V2 field and session schema names remain compatible with existing files.

The website runs separately with `npm --prefix website ci && npm --prefix website run dev`.
