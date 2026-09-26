---
name: reaction-verify
description: Verify immutable candidate molecular structures against an authoritative reaction figure using source crops, RDKit/E-SMILES renders, strict stereo audits, and fail-closed FHR closure without GT or alternate-lane anchoring.
---

# Source-grounded reaction structure verification

Use this Skill in a fresh session after an extractor or annotator has emitted an immutable candidate. The source image is authoritative. The verifier emits a patch; it never overwrites the candidate.

## Information boundary

Allowed:

- authoritative source image and deterministic source crops;
- exact candidate molecule paths and strings;
- optional human MASK scope;
- deterministic outputs produced from the candidate or a proposed correction.

Forbidden while creating the source judgment:

- GT or accepted reference answers;
- scorer output;
- extraction transcript;
- another model/system's candidate string;
- prior adjudication conclusions.

A hidden disagreement may raise audit priority but may not reveal the other lane's answer.

## Required evidence order

For every in-scope occurrence:

1. Open the full source and MASK overlay.
2. Open the enlarged source crop.
3. Describe the drawn atoms, bonds, chain/ring topology, substituent positions, components, charge, and visible stereo before relying on deterministic chemistry output.
4. Call `verify_smiles` for every suspected error, proposed correction, assigned-stereo structure, tagged E-SMILES, and high-risk long-chain/ring/charge/coordination structure.
5. Open both `render_png_path` and `indexed_render_png_path` returned by `verify_smiles`. Compare the rendered candidate directly with the source crop. Do not rely only on canonical text, formula, or atom count.
6. If source evidence uniquely supports a correction, call `verify_smiles` on the exact proposed string and open its two PNG renders before emitting it.
7. Keep ambiguity `unresolved`.

## Mandatory checks

### Chain length

Explicitly enumerate every unlabeled endpoint and vertex. State which labeled atom, carbonyl atom, or aromatic attachment atom is excluded from the methylene count. Compare the source count with the atom-indexed render and formula.

### Rings and regiochemistry

Count ring atoms and substitution positions from a fixed source anchor. Inspect fused and bridged ring closures in the indexed render.

### Tetrahedral stereo

Identify the exact wedge/dash center and its four source substituents. Use `verify_smiles` CIP output only after mapping those substituents. Never flip `@`/`@@` by textual intuition.

### Alkene stereo

Map both substituents at both alkene atoms before accepting E/Z. Check `double_bond_stereo` and atom indices against the source.

### Charge, counterions, and coordination

Inventory every source component and formal charge. A parseable disconnected string is not proof that all source components were captured.

### Extended-SMILES

Fail closed on unknown tags, unresolved labels, open dummies, unsupported abbreviation expansion, partial rendering scope, or failed concrete resolution. Preserve the immutable input string.

## FHR verification

For a source-visible Markush/template relationship:

1. call `reverse_rgroup(template, concrete)`;
2. call `fill_rgroup(template, r_map)` with the returned map;
3. compare the roundtrip with the candidate using connectivity and strict stereo separately.

Use `reverse_assemble` only when the source supplies the concrete product and generic product/reactant templates. Partial, dummy-bearing, unsupported, or non-unique output fails closed.

FHR closure proves only deterministic consistency with the supplied strings. It cannot prove that the source was read correctly and cannot resolve source ambiguity.

## Output contract

Emit an auditable patch record with:

- candidate path;
- exact before string;
- keep/replace/add/remove/unresolved;
- exact after string only when uniquely source-established;
- source crop and evidence note;
- connectivity and strict-stereo verdicts;
- `verify_smiles` status and render paths;
- FHR status when applicable.

Never mutate the raw candidate. Promotion requires an independent challenge and must track verifier fixes and verifier-induced breakages separately.
