# LESSONS — accumulated GENERAL extraction rules (read together with SKILL.md)

These are *rules of process*, distilled from scored errors on the TRAIN split, never figure-specific
answers. Each entry is a general instruction that improved held-out VAL when added. Do not put any
concrete SMILES or figure id here (the loop runs `tools/skill_lint.py` and rejects it). Keep entries
short and imperative.

## Run ownership and evaluation hygiene
- **The current Pi is the extractor.** It must read the Skill, inspect image evidence, make every chemical
  decision, and call the canonical tools in one attributable run. Never substitute an external one-shot
  VLM, nested agent, workflow/subagent, or analyst-authored answer and report it as Pi-direct extraction.
  Deterministic helper code may store data or validate mechanical graph closure, but may not hide a second
  chemical decision-maker.
- Seal the raw prediction before GT access. Report the sealed agent score separately from every post-seal
  Ketcher/manual correction; reviewed uplift measures human correction, not agent accuracy.

## Format & fields
- **identifier = the printed compound number/letter beside the structure** (e.g. a numeral, or a
  numeral+letter). If a cell has no printed number, fall back to `p<bbox_index>` — NEVER invent a
  semantic placeholder like `selected-example-N`, `entry-N`, or `scope-N`. (Placeholder identifiers
  were ~40% of one agent's outputs and are always wrong against GT.)
- If a drawn reactant has no compound number but the figure title/caption explicitly prints its
  chemical name, use that printed name as the identifier; use `p<bbox>` only when neither a number
  nor an explicit name is available.
- **Do not over-materialize the templates.** When you enumerate a scope grid into concrete cells,
  still emit the generic `reaction_templates` (markush) block — keep its wildcards. The general-form
  score reads `reaction_templates`; dropping/flattening it into only concrete examples loses that
  credit even when the concrete molecules are correct.
- Every example must be `reactants -> products` complete; never emit an example with an empty
  reactant list or empty product list.
- **Optimization tables require one concrete example per printed row.** Reuse the shared molecule
  sets, but attach each row's conditions and metrics to its own `selected_examples` item;
  `optimization_runs` may supplement these rows but must not replace them.

## Reading structures
- **Concrete (markush:false) detections: use OCSR by default, but reconcile explicit image conflicts.**
  Re-typing a whole drawn structure is net-negative. However, when the compare grid/crop explicitly
  labels a substituent, ring size, charge, or mapped stereobond that conflicts with OCSR, correct only
  that localized feature, verify the corrected SMILES, retain verbatim `molparser_esmi`, and record a non-empty
  `ocsr_override:{reason,evidence}`. Open every enlarged concrete audit crop and record
  `ocsr_audit:{decision,evidence}`; assembly fails closed when a raw concrete read lacks this audit.
  Never mutate the raw MolParser cache.
- **Double-check bulky/similar alkyl groups against the crop.** Look-alike substituents (e.g. a
  tert-butyl vs an iso-amyl vs an iso-butyl group) are a recurring shared misread; printed n-/i-/t-
  labels are authoritative evidence and must be reconciled before propagating a repeated scope group.
- For a long unbranched chain, count every visible zig-zag vertex in the enlarged crop and compare it with
  the OCSR rendering. A high OCSR confidence score does not validate chain length or excuse a one-carbon
  insertion/deletion.
- **Tagged E-SMILES preservation is not concrete validation.** For a catalyst, counterion, ligand, or
  other concrete tagged structure, run `verify_smiles` with `require_concrete:true`. A parse of only the
  wildcard body does not prove the extension labels were expanded or charge-balanced. If labels/tags are
  unsupported or dummies remain, retain the raw OCSR string only as provenance and either make a localized
  image-grounded concrete override or fail closed; never publish the unresolved body as concrete chemistry.
- **A drawn coordination closure is chemical connectivity, not optional decoration.** If a crop explicitly
  shows an O→B, N→metal, or other dotted/dative closure but OCSR omits it, `ocsr_audit.decision:"accepted"`
  is forbidden: create an image-grounded override, verify a charge-balanced representation, and preserve
  the closure in every applicable concrete cell. For an N-oxide/borane adduct, a neutral open borane plus
  disconnected O− is incomplete; use a valid charge-separated B−–O–N+ representation when supported, or
  fail closed and report the unsupported coordination instead of silently dropping the bond. If otherwise
  analogous scope products inconsistently contain or omit the closure, treat that as an OCSR conflict and
  reconcile every row against its own enlarged crop.

## Deriving undrawn reactants
- **Prefer the reactant that is actually drawn.** If a reactant is drawn (a fixed coupling partner,
  a shown precursor), use its detection; only derive by reverse-assemble / mechanism reasoning the
  reactants that are NOT drawn. GT usually lists the drawn precursor, so a mechanistically-split
  intermediate can score as a mismatch even when it is chemically equivalent.
- Deterministic reverse-assemble and model mechanism-reasoning give ~equal reactant accuracy; use
  reverse-assemble first for a clean R-substitution scope, fall back to reasoning when it returns
  partial/none. Exact product roundtrip is necessary but not sufficient: reject a degenerate partition
  where a named variable collapses to hydrogen/a trivial fragment or another reactant absorbs essentially
  the whole product without image support.
- An explicit intermediate followed by a new condition arrow is a separate reaction step. Emit it and use
  that intermediate as the next step's reactant; do not skip directly from the original precursor to the
  final product merely because the final product can be reconstructed. Count every visible arrow before
  planning: when a later panel reuses a prior generic product, set `is_multistep:true` and connect the
  steps in `reaction_graph`; multiple templates do not excuse an empty graph.

## Stereochemistry: verify before declaring a perception ceiling
- **An explicit R/S/E/Z label or wedge/dash is actionable evidence.** Run `verify_smiles`, inspect the
  atom-indexed rendering, and compare RDKit CIP/E-Z to the figure. Correct only a mapped, unambiguous
  mismatch and verify again; never blindly flip SMILES stereo characters.
- Every product element must have a recorded reactant source. Atom-transferring reagents such as
  diboron, silaborane, halogenating, alkylating, and acylating partners remain reactants even when
  printed above the arrow; leaving them only in conditions creates an incomplete reaction record.
  If such a reagent is text-only rather than structurally drawn, use `bbox_inherit:false` so the
  reviewer does not mislabel a neighboring product box as evidence for that reagent.
- Every potentially stereogenic alkene requires a mapped `stereo_audit`. Compare the two substituent
  pairs in the enlarged crop, verify the candidate with RDKit E/Z, and record `specified` plus `rdkit_ez`;
  otherwise explicitly record `unspecified` or `ambiguous`. Never inherit slash notation without this audit.
- Treat strict stereo as a perception ceiling only when the drawing is genuinely unreadable, ambiguous,
  or does not specify it. A printed descriptor can itself conflict with a drawn stereobond: for an
  image-extraction task, preserve the mapped drawing in the structure, retain the printed descriptor as
  text, and flag the conflict rather than silently changing either. Report nostereo and strict-isomeric
  scores separately; a nostereo match is not a fully correct structure.
