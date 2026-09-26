# RxnMiner figure skills

A directly installable Codex plugin for extracting **every reaction from one
chemistry figure** into validated V2 JSON.

The model reads the figure and performs the chemical reasoning. The bundled
`chem-extract` MCP server supplies deterministic support for OCSR, R-group
reconstruction, SMILES validation, strict V2 assembly, and visual review.

## Distributed blind extraction

For a frozen multi-account run, the coordinator uses
`distributed/PREPARE_COORDINATOR.md` to stage image-only benchmark data, freeze one MolParser cache per
figure, and create balanced GT-free shards. Each participant gives the shard-local
`RUN_CODEX.md` to Codex; Codex performs only orchestration while a fresh Pi session with
`reaction-extract` remains the sole chemistry decision-maker. The shard runner pins the repository,
provider/model/thinking, immutable perception, concurrency 1–2, output validation, sealing, and a
checksummed submission archive.

Credentials are never packaged. On a shared server, participants must use separate Unix accounts and
private `PI_CODING_AGENT_DIR` directories; sharing one Unix account is not a credential-security
boundary.

## One-command extraction

After Pi is installed and authenticated, one script runs an isolated, inspectable extraction:

```bash
./figure_skills/scripts/run_pi_extract.sh /absolute/path/to/figure.png
```

It accepts a directory for batch extraction, supports immutable MolParser caches and offline replay,
limits concurrency to one or two, refuses to overwrite outputs, disables repository context discovery,
and loads only the canonical runtime Skill and native chemistry extension. Run `--help` for all options.
A cache miss requires a reachable MolParser-compatible endpoint (`MOLPARSER_URL` can override it).

## Use in Pi

The monorepo root includes `.pi/settings.json`, which discovers this package's
`reaction-extract` skill and native Pi extension after the project is trusted.
Start Pi from the repository root, then attach a figure and ask:

> `/skill:reaction-extract` Extract every reaction in this figure into V2 JSON.

You can force progressive loading with `/skill:reaction-extract`. The Pi
extension exposes the same eight chemistry tools as the Codex MCP plugin and
lazily starts `scripts/run_chem_mcp.py`; chemistry logic remains in the shared
Python MCP implementation rather than being duplicated in TypeScript.

For a one-off checkout that does not use the project settings:

```bash
pi --skill figure_skills/skills/reaction-extract \
  --extension figure_skills/pi-extension/index.ts
```

## Optional post-seal verification

`skills/reaction-verify/SKILL.md` is a separate source-grounded verifier for an immutable extraction
candidate. It emits `keep|replace|add|remove|unresolved` patch proposals; it does not overwrite the
candidate and is intentionally not loaded into Product Pi's extraction allowlist. Invoke it explicitly
in a fresh, reference-blind session after sealing when verification is required. `verify_smiles`
automatically attaches clean and atom-indexed PNG renders, so visual comparison does not depend on a
second manual file-read action.

## Use the chemistry tools from a shell

The stable CLI calls the same MCP boundary used by Pi and Codex, so it is useful
for CI, debugging, and agents without native Pi-extension support:

```bash
python figure_skills/scripts/chem_tool.py list
python figure_skills/scripts/chem_tool.py call verify_smiles \
  --args-json '{"smiles":"CCO"}'
python figure_skills/scripts/chem_tool.py call detect \
  --args-file /tmp/detect-args.json
python figure_skills/scripts/chem_tool.py call assemble \
  --args-json '{"plan_path":"/tmp/plan.json","out_path":"/tmp/out.v2.json"}'
```

Use `--raw` to retain the complete MCP tool-result envelope. The default output
prints the decoded JSON payload and returns exit status 2 when the MCP tool
reports `isError`.

## Install in Codex

This repository is both a plugin and a one-plugin marketplace:

```bash
codex plugin marketplace add rxnminer/RxnMiner
codex plugin add rxn-skills@rxn-skills
```

The repository is private, so Git must already have access to
`rxnminer/RxnMiner`. Restart Codex after installation.

The first chemistry-tool call automatically creates a private Python virtual
environment under `~/.cache/rxn-skills/venv` and installs the packages from
`requirements.txt`. Set `RXN_SKILLS_CACHE_DIR` to put this runtime elsewhere.

## Use

Attach or provide the path to one chemistry figure and ask:

> Use $reaction-extract to extract every reaction in this figure into V2 JSON.

Codex discovers the `reaction-extract` skill and its `chem-extract` MCP
dependency from the plugin manifest. The workflow is:

1. Run `detect`, then inspect the generated overview and OCSR comparison images.
2. Use OCSR reads for drawn molecules and reconstruct only undrawn scope
   reactants.
3. Read compound numbers, yields, stereochemical metrics, and conditions from
   the figure.
4. Run `assemble` to create strict V2 JSON.
5. Run `render_reactions` and visually review the extraction.

`detect` is cache-first. It checks `<image-stem>.molparser.json`, a sibling
`molparser.json`, and `$OCSR_CACHE/<md5-of-image>.json` before making any network
request. For a genuinely new image it calls the bundled default
MolParser-compatible HTTP endpoint directly and atomically saves the exact raw
detections beside the image for future runs. The tool result reports
`ocsr.source`, `ocsr.cache_hit`, `ocsr.cache_path`, and
`raw_molparser_json`, so cache and live perception are never conflated.

Override the endpoint when the default is unreachable:

```bash
export MOLPARSER_URL="https://your-host.example/extract-mol"
python figure_skills/scripts/chem_tool.py call detect \
  --args-json '{"image_path":"/absolute/path/to/new-figure.png"}'
```

MolParser is a direct HTTP service and must not be routed through SOCKS. Set
`OCSR_OFFLINE=1` for sealed/offline replay: a missing cache then fails visibly
instead of attempting the endpoint. `force_live:true` exists for an explicit
refresh, but normal extraction must not repeat OCSR for a cached image.

## What is included

- `skills/reaction-extract/SKILL.md` — extraction workflow and output contract.
- `skills/reaction-extract/LESSONS.md` — general lessons retained by held-out
  validation.
- `skills/reaction-extract/agents/openai.yaml` — Codex UI metadata and MCP
  dependency declaration.
- `.codex-plugin/plugin.json` and `.mcp.json` — installable Codex plugin definition.
- `pi-extension/index.ts` and the root `.pi/settings.json` — native Pi skill discovery and typed tool bridge.
- `tools/chem_mcp.py` — eight-tool stdio MCP server.
- `scripts/run_chem_mcp.py` — dependency bootstrap and server launcher.
- `scripts/chem_tool.py` — stable shell/CI client for the same MCP boundary.
- `engine/` and `third_party/` — bundled R-group assembly implementation.
- `docs/HANDOFF_skill_iteration_loop.md` — leakage-safe train/VAL/TEST ratchet.
- `docs/pdf_oa50_skill_data.md` — reviewed evaluation-data instructions.

The eight MCP tools are `detect`, `ocsr_detect`, `reverse_rgroup`,
`fill_rgroup`, `reverse_assemble`, `verify_smiles`, `assemble`, and
`render_reactions`.

## Develop and verify

Run the structural validators:

```bash
python3 /path/to/plugin-creator/scripts/validate_plugin.py .
python3 /path/to/skill-creator/scripts/quick_validate.py skills/reaction-extract
python3 tools/skill_lint.py skills/reaction-extract/SKILL.md
python3 tools/skill_lint.py skills/reaction-extract/LESSONS.md
```

Start the MCP server directly:

```bash
python3 scripts/run_chem_mcp.py
```

It speaks newline-delimited JSON-RPC over standard input/output. Logs and
dependency-install output go only to standard error.

Or run the bundled offline smoke test:

```bash
python3 scripts/smoke_test.py
```

## Iterating the chemistry rules

Follow `docs/HANDOFF_skill_iteration_loop.md`: inspect training examples one at
a time, distill one general rule, lint it, and keep it only when the frozen VAL
`all_molecule_f1_ns` increases. TEST remains sealed. Never place benchmark IDs
or answer SMILES in the skill.

See `ATTRIBUTION.md` for the bundled molecular-assembly attribution and
redistribution constraints.
