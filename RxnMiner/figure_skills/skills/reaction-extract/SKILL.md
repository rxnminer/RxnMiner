---
name: reaction-extract
description: >
  Extract every reaction from ONE chemistry figure (a scheme, a substrate-scope table, an
  optimization table, a multistep sequence) into structured V2 JSON. Read-first: OCSR
  (MolParser) reads the drawn structures for you and you USE those reads; the fhr R-group
  engine derives the parts that are only drawn as a generic template; you read the printed
  numbers and conditions off the image; the engine assembles the strict V2 so you never
  hand-hold the schema. Use when the user gives a reaction figure and wants it extracted.
---

# Reaction extraction (read-first)

This canonical Agent Skill is shared by Claude Code, Codex, and Pi after `./setup`. Invoke it as
`/reaction-extract`, `$reaction-extract`, or `/skill:reaction-extract`, respectively. The harness adapter
changes, but the Skill, `chemrxn-extract` chemistry server, FHR engine, V2 contract, and evidence rules do
not. Record the actual harness/model in the seal manifest; never label a Claude/Codex run as Pi-direct.

Before doing anything else, read `LESSONS.md` in this skill directory completely
and apply its general extraction rules throughout the task. When `detect` returns any
`esmi` containing `<sep>` or extension tags, read
`references/extended-smiles-2.0.md` before interpreting its labels or using reverse/fill.

You are extracting **one figure** into our V2 JSON. The core idea: **do not re-draw structures
that are already drawn.** OCSR is the default transcription, but the compare grid can expose a
specific conflict with explicit image evidence; correct that conflict instead of blindly copying it.
Your job is to *bind* (which structure is which, its number, its role, its conditions)
and to *derive* only the parts that are not drawn — then let the engine format the V2.

## Extractor ownership (non-negotiable)

**You, the current Claude Code, Codex, or Pi session, are the extractor.** Read this Skill and its
delegated references, inspect the image/evidence yourself, make the chemical decisions, call the canonical
chemistry tools, and author the pre-GT plan in this same run. Do not hand extraction to a one-shot VLM,
Gemini/baseline endpoint, nested agent, workflow/subagent, or hidden helper model. A deterministic script
may store a large plan, perform a mechanical graph transform, or validate an exact roundtrip, but it must
not become an unrecorded second chemical decision-maker. Keep every chemical decision and tool outcome
attributable to this recorded agent session.

This does not forbid visual reasoning: MolParser reads drawn structures, while you must read printed text,
roles, colors, conditions, identifiers, and crop evidence from the figure. It forbids outsourcing that
reasoning to another model and then presenting the result as direct extraction by the current harness.

## Output contract (non-negotiable)

The deliverable is the **assembled V2** (`assemble(plan)` or, for a large saved plan, `assemble(plan_path=...)` → `v2_path`). It is not done until:

- **Every example has BOTH reactants AND products.** A reaction is `reactants → products`; an
  example with `reactants: []` is INCOMPLETE and unacceptable. Deriving the reactants (below) is
  the core task, not optional polish — most figures draw the *products* of a scope table and
  leave the per-cell *reactants* for you to reconstruct.
- Every numbered cell / arrow / table row is covered, each with its printed yield.
- **An optimization table is row-level reaction data:** emit one `selected_examples` item per
  printed entry, even when all rows share the same reactants/products. Repeat the shared molecule
  sets and bind that row's catalyst/base/solvent/temperature and metrics to that example. Do not
  represent the rows only inside `optimization_runs`: that supplementary field does not replace
  row-level reactions for counting, scoring, or review.
- You do the reading/deriving; the native `assemble` tool does the V2 formatting. Never hand-emit V2 or import/call the Python engine through `bash`; save a large plan as JSON and pass `plan_path`.
- For an evaluated run, seal the raw prediction before GT access and record the extraction method
  (`claude-code-direct`, `codex-direct`, or `pi-direct`), Skill/repository revision, current model boundary,
  and canonical tools used. Report the sealed agent score
  separately from every post-seal Ketcher/manual correction. A reviewed uplift is not agent accuracy.

## Tools (MCP server `chem-extract`)

| tool | what it does | when |
|---|---|---|
| `detect(image_path)` | cache-first OCSR + visualization: reuses saved MolParser detections, or calls live MolParser for a new image and saves the raw result; writes an **annotated overview PNG**, **OCSR compare grid PNG**, and one enlarged raw **concrete audit crop** per concrete bbox | **first** — OPEN both overview/grid and EVERY concrete audit crop |
| `reverse_rgroup(template, concrete)` | read the R-substituents off one drawn cell given the generic template | a markush scope cell |
| `fill_rgroup(template, r_map)` | materialize a concrete molecule from a template + R-map (batch: pass a JSON list) | build undrawn reactants/products of a scope grid |
| `reverse_assemble(target_product, product_template, reactant_templates)` | derive ALL of one cell's concrete reactants in one call (peels the product's R-groups, fills every reactant template) — derives organometallics (alkylzinc etc.) the per-cell reverse+fill under-peels to None | the preferred deterministic route for a scope grid's undrawn reactants |
| `verify_smiles(smiles, require_concrete?)` | RDKit validity + canonical forms + tetrahedral CIP R/S + double-bond E/Z + indexed SVG; for tagged Extended-SMILES also reports unsupported labels/tags, expansion, and unresolved dummies | every explicit stereolabel/wedge/dash; use `require_concrete:true` for catalysts and all other concrete tagged structures |
| `assemble(plan)` or `assemble(plan_path=...)` | turn your intermediate plan into strict V2 (preserves accepted MolParser `esmi` verbatim, canonicalises agent-built chemistry, recovers printed numbers); use `plan_path` for large tables | **last** |
| `render_reactions(v2_path, image_path)` | render the V2 to an HTML you can look at | to sanity-check the result |

## Workflow

1. **`detect(image)` and OPEN `overview_png`, then `compare_grid_png`, then EVERY path in
   `concrete_audit_crops`.** Every drawn structure is
   boxed with its `bbox_index` and a confidence colour (green ≥0.9 = trustworthy). This is your map of
   what is drawn and where each printed number/yield sits. `detect` handles MolParser automatically:
   it first checks `<image-stem>.molparser.json`, sibling `molparser.json`, then
   `$OCSR_CACHE/<md5-of-image>.json`. If no valid cache exists, it calls the configured live
   MolParser service directly and atomically saves the exact raw detections for later runs. Read the
   returned `ocsr.source`, `ocsr.cache_hit`, `ocsr.cache_path`, and `raw_molparser_json`; never claim a
   cache hit when the source says `live_molparser`. The immutable raw JSON retains MolParser's
   `caption`, `smi`, and `esmi`, but the extraction contract exposes and carries forward **only `esmi`**.
   Never canonicalize or reserialize an accepted MolParser `esmi`: atom ordering and `<a>idx:label</a>`
   bindings are display/provenance data. Preservation is not semantic validation: for a structurally
   drawn catalyst/condition or any other structure that must be concrete, call
   `verify_smiles(full_esmi, require_concrete:true)`. If the report contains unsupported labels/tags,
   open Markush slots, remaining dummies, or `rendering_scope:"body_only_partial"`, do not place that
   unresolved string in the concrete `E-SMILES` field. Keep it byte-verbatim only as
   `_source.ocsr_esmi`, preserve the printed condition text, and either make a localized image-grounded
   concrete override that passes verification or fail closed as unresolved. A wildcard body parse is
   never proof that labels such as a counterion/ligand were expanded or charge-balanced. **Before using any concrete read, compare the left
   `drawn (figure)` panel with the rendered `OCSR esmi (verbatim)` panel. Explicit printed substituent
   text such as n-/i-/t- alkyl, Et/Me, ring size, charge, or a mapped stereobond is a mandatory conflict
   check, not optional polish.** For each concrete molecule, put `molparser_esmi` and
   `ocsr_audit:{decision:"accepted"|"override", evidence:"explicit content read from that raw crop"}`
   in the plan. For every potentially stereogenic alkene, also record
   `stereo_audit:{decision:"specified"|"unspecified"|"ambiguous", evidence:"mapped crop geometry", rdkit_ez?:"E"|"Z"}`.
   A `specified` decision requires generating/verifying a candidate with `verify_smiles` and reporting
   the RDKit E/Z assignment; assembly fails closed if a potential alkene lacks this mapped audit.
   A generic statement such as "looks correct" is not evidence: transcribe the local
   substituent/ring/charge/stereo feature you checked. `assemble` fails closed when raw concrete OCSR is
   attached without this audit. Do not call MolParser again in the same extraction.
   Use `force_live:true` only when the user explicitly requests a refresh or the cached image identity
   is known to be wrong. For offline/sealed work, set `OCSR_OFFLINE=1` so a cache miss fails visibly.
   If a new image cannot reach the default endpoint, set `MOLPARSER_URL` to a reachable compatible
   `/extract-mol` service and rerun `detect`; do not route the MolParser request through SOCKS.

   **Create an independent source inventory before binding detections to reactions.** Scan the full
   figure, not only the boxes returned by MolParser, and write one short ledger entry for every visible
   molecular region, reaction arrow, table/scope cell, and explicitly excluded or masked region. A
   missed detection must not become an invisible omission. Before assembly, every ledger entry must be
   accounted for as emitted, explicitly excluded, masked, or unresolved. This inventory records visible
   regions and disposition only; it is not permission to guess a hidden structure.

2. **Concrete cells (`markush:false`): use the detection's connectivity, then audit visible stereo.**
   A `markush:false` detection is a fully-read structure — copy its `esmi` byte-verbatim rather than re-typing or canonicalizing the
   skeleton. But `markush:false` and a high OCSR score do NOT prove strict stereochemistry. Whenever the
   figure shows a wedge/dash, alkene geometry, or a printed R/S/E/Z descriptor, call `verify_smiles` and
   compare its automatically attached clean and atom-indexed PNG renders plus reported atom CIP / bond
   E-Z to the figure. If either expected image attachment is unavailable, fail closed rather than
   treating text-only RDKit metadata as a completed visual audit. If the SMILES
   conflicts with unambiguous image evidence, correct only the evidenced atom/bond/substituent and verify
   the corrected SMILES. Preserve `molparser_esmi` as the immutable raw read and add
   `ocsr_override:{reason:"...", evidence:"..."}` to that molecule in the plan; this is the only lane
   that allows the assembly guard to keep a concrete correction instead of restoring OCSR. Never use an
   override for a vague visual impression, and never mutate the cached/raw MolParser JSON. For stereo,
   never blindly flip `@` or `/` characters without mapping the indexed atom/bond. If a printed R/S/E/Z
   descriptor conflicts with the mapped wedge/dash geometry, do not silently force either one to agree:
   preserve the drawn geometry in the structure, preserve the printed descriptor in the
   identifier/description, and record the conflict for review. If no explicit stereo evidence is visible,
   retain the MolParser read and record the ambiguity instead of guessing.

3. **Generic templates (`markush:true`): reverse + fill.** First apply
   `references/extended-smiles-2.0.md`: tag positions belong to the original non-canonical
   MolParser traversal, so preserve the whole `esmi` and never renumber or rewrite its tags.
   The top scheme usually draws the generic reactants/products as templates (with `*` and `<a>idx:label</a>` tags). For a scope
   cell, read its varying groups off the figure, build the R-map, and `fill_rgroup` to
   materialize — do NOT hand-type each concrete product (hand-typing scrambles which group lands
   at R1 vs R2). Use `reverse_rgroup` to read a cell's R off a drawn concrete when helpful.

4. **Read off the IMAGE (not from OCSR):**
   - `identifier` = the **printed compound number** beside the structure (e.g. `39`), **not** the
     `bbox_index`. If no number is printed but the title/caption explicitly names that molecule,
     use the printed chemical name; only then fall back to `p<bbox_index>`.
   - `metrics` = the printed yield / ee / dr (keep the `%`).
   - `conditions` = the arrow gives the standard set; **per-cell superscript letters (a,b,c…)
     map to footnote variations** — capture each cell's letters and read the footnote mapping.
     Never emit a condition as a bare number.
   - `amount` = a reactant's drawn stoichiometry (`1.2 equiv`) if shown.
   - `description` = what distinguishes this cell (its R-values / scope variable / table entry).
   - stereochemistry = transcribe every visible R/S/E/Z label and wedge/dash relationship, then validate
     it against `verify_smiles` CIP/E-Z output before assembly.

5. **Derive each cell's reactants (REQUIRED — the hard, essential part).** A scope table draws the
   generic reaction once (the top scheme: reactant templates + arrow) and then draws only each
   *product*. Reconstruct every cell's reactants. **Two routes — pick by the scope type:**

   **(a) Mechanism reasoning (DEFAULT — use for multi-component or topology-varying scopes).**
   Read the top scheme to understand the transformation (which reactant contributes which fragment,
   at which position). Then for each product, DECOMPOSE it by mechanism: work out which piece of
   the product came from which reactant template, and write each reactant's CONCRETE SMILES —
   including its regiochemistry (which carbon carries which group). This is real retrosynthesis, and
   the model does it well where pure graph-matching fails (e.g. an organozinc `R-[Zn]Br`, or a
   product whose ring/topology differs cell-to-cell). Sanity-check: mentally re-run the forward
   reaction on your three reactants — it must reproduce the drawn product. `verify_smiles` each
   reactant.

   **(b) Deterministic reverse-assemble (use for an R-substitution / scope grid).** For a cell,
   call `reverse_assemble(concrete_product, product_template, [reactant_templates])` — it peels the
   product's R-groups against the product template and fills EVERY reactant template in one call,
   returning that cell's concrete reactants (aligned with the templates). This is the correct fhr
   entry and derives organometallics (e.g. an alkylzinc `Br[Zn]C1CCC1`) that the older per-cell
   `reverse_rgroup`+`fill_rgroup` path under-peels to None. Copy any fixed reactant from its
   detection `esmi`. (`reverse_rgroup`/`fill_rgroup` remain for reading/building a single R-position
   when you want finer control.) If a reactant comes back `partial`/None (e.g. an under-tagged aryl
   ring substituent), fall back to mechanism reasoning (a) for that reactant. Even when it reports an
   exact product roundtrip, reject a chemically degenerate partition where a named variable collapses
   to hydrogen/a trivial fragment or one reactant absorbs essentially the whole product without image
   support. Exact closure is necessary, not sufficient: each variable fragment must be nontrivial when
   the drawing says so and grounded by the template/crop.

   Classify every derived reactant as `drawn`, `uniquely_derivable`, or `unresolved`. Mechanistic
   plausibility, product closure, or FHR roundtrip alone cannot upgrade a reactant to
   `uniquely_derivable`; the source must constrain it to one structure. Never leave `reactants: []`.
   If a reactant is genuinely underivable, emit its template smi (with `*`) and an unresolved note
   instead of guessing a concrete partner. Mechanism reasoning may produce a concrete reactant only
   when the source plus transformation uniquely determine it.

6. **Build the intermediate plan** — one entry per numbered cell **or optimization-table row**,
   WITH reactants populated. For an optimization table, the printed Entry number is the
   `instance_id`; reuse the shared drawn reactants/products in every row and put conversion,
   product ratio, yield, and that row's conditions on the same example. Preserve every printed
   metric even if one metric is absent in another annotation source. Then build:
   `{selected_examples:[{reactants:[{smiles,identifier,bbox_index,bbox,amount}], products:[{smiles,
   identifier,bbox_index,bbox,bbox_inherit?,molparser_esmi?,ocsr_audit:{decision,evidence},ocsr_override?:{reason,evidence},stereo_audit?:{decision,evidence,rdkit_ez?}}], conditions:[[{role,text,smiles?,bbox_index?,bbox?}]], identifier,
   description, markush_enumeration:{R1:"*frag",...}}], reaction_templates:[...]}`. For every drawn
   molecule (including a structurally drawn condition), copy BOTH `bbox_index` and the four-coordinate
   `bbox` from `detect`; do not make downstream viewers re-query OCSR to recover localization. Put every
   atom-contributing reagent in `reactants` even if it is printed above the arrow or beside conditions;
   catalysts, solvents, bases, and non-transferred additives remain conditions. For an atom-source
   reagent printed only as text and not as a drawn structure, set `bbox_inherit:false`; never attach a
   neighboring product crop merely to satisfy bbox coverage.

   Before deciding topology, write an arrow ledger: one line per visible reaction arrow with its input
   structure(s), output structure(s), and whether that output is reused downstream. If any later arrow
   consumes a structure produced by an earlier arrow—even when the repeated structure is still generic
   and the later panel merely varies a metal/reagent—this is a chained synthesis. Set
   `is_multistep:true`, add a `synthesis_overview`, give the linked templates/examples `_multistep`
   metadata, and emit one `reaction_graph` edge per arrow using the same molecule identifiers as the
   examples. An explicitly drawn/named intermediate plus a new condition arrow is a separate reaction
   step: use that intermediate as the next step's reactant rather than coupling the original precursor
   directly to the final product. Multiple independent alternative arrows may remain single-step, but
   the arrow ledger must explicitly say that no product is reused.
   For yields/ee/dr,
   set metric `of` only when the number is printed for one specific product; leave a combined row yield
   unlinked in a multi-product reaction.

7. **Self-check before assembling:** no example may have empty `reactants`. Reconcile the independent
   source inventory: every visible molecular region, arrow, and numbered/table cell must have exactly
   one disposition (`emitted`, `explicitly_excluded`, `masked`, or `unresolved`). Recount the visible arrows
   against `reaction_graph`; if the ledger says a product is reused downstream, `is_multistep:false` or
   an empty graph is a blocking error, not a warning. For every tagged concrete condition/molecule,
   require `verify_smiles(..., require_concrete:true).valid == true`; ordinary RDKit validation of only
   the text before `<sep>` does not satisfy this check. Confirm that every
   concrete molecule carrying `molparser_esmi` also carries a crop-grounded `ocsr_audit`; do not mark a
   crop accepted until you have opened it and transcribed its distinguishing local feature. For every
   potentially stereogenic alkene, map the two substituent pairs in the crop, record `stereo_audit`,
   and, when specified, save the `verify_smiles` E/Z result and ensure it agrees. Do not infer geometry
   from raw slash characters alone; if the crop genuinely does not specify it, record `unspecified`.
   Confirm that every element appearing in a product has a source among the recorded reactants. A
   missing B, Si, halogen, metal, or other atom source usually means an atom-transferring reagent was
   incorrectly left only in conditions; assembly fails closed in that case. For long unbranched chains,
   count every visible zig-zag vertex across the enlarged crop and compare it with the OCSR rendering;
   high confidence does not excuse a one-carbon insertion/deletion.
   Finish all `verify_smiles` decisions, then call the native **`assemble(plan)`** tool (or save a
   large plan and call **`assemble(plan_path=...)`**) → strict V2, followed by `render_reactions` to
   eyeball the result. Any `verify_smiles` call after assembly makes that candidate stale: apply the
   source-grounded decision to the plan, assemble again, and render again. Directly drawn molecules
   must retain `bbox_index`, exact `molparser_md5`, and raw MolParser E-SMILES; prose such as “from
   bbox 11” is not machine-readable lineage. Fix and re-assemble until every example is complete and
   every visible stereodescriptor has been checked. Seal this
   extraction as the immutable candidate before any optional verification stage. A fresh
   `reaction-verify` session may inspect the source plus candidate and emit a patch, but must not
   overwrite the candidate or read reference/alternate-lane answers.

8. **Open the sealed result in the canonical Chem Reviewer.** For a direct Claude Code, Codex, or Pi
   run outside the Product runtime, use the installed adapter after sealing:
   `chemrxn review output/pred.v2.json --image input/image.png --perception
   output/detect/image_molparser.json`. This imports the immutable candidate into the orange/white Chem
   Reviewer and opens its Figure task with source image, bbox overlay, molecule provenance, templates,
   and concrete reactions. Do not present the legacy dark Canvas or only the standalone
   `render_reactions` HTML as the primary visual result. Product-managed `chemrxn extract` sessions are
   already registered and must not be imported a second time.

## Rules

- **The drawing is the authority; OCSR is the default transcription, not ground truth.** Do not re-type
  a whole structure from scratch, but do correct a localized conflict supported by explicit image
  evidence through `ocsr_override`, retaining the raw OCSR read for audit.
- **identifier = printed number, never the bbox index.**
- **Cover every numbered cell / arrow / table row** — one example each, with its yield.
- **Reactants that are not drawn** (implied coupling partners, the standard partner of a scope
  panel) you derive by reverse/fill or by reasoning — do not leave them empty, but do not invent
  a structure the chemistry does not support. Diboron, silaborane, halogenating, alkylating, acylating,
  and other atom-transfer reagents belong in `reactants`, not only in `conditions`.
- **You produce understanding; the engine produces V2.** Don't fight the schema — put what you
  read into the plan and let `assemble` format it.
