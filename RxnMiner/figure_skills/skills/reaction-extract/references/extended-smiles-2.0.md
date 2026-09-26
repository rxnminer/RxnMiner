# MolParser Extended-SMILES 2.0: extraction reference

Use this reference when a MolParser `esmi` contains `<sep>` or extension tags. It explains the wire format; it does not authorize rewriting the raw string.

## Extraction rule

MolParser `esmi` is ordered protocol data, not merely a molecule-equivalence string.

- Preserve an accepted detection byte-verbatim in `molparser_esmi`, final `E-SMILES`, and `_source.ocsr_esmi`.
- Never canonicalize, reorder, regenerate, or manually repair its body or tags.
- Use canonicalization only for validation/equality, without writing the canonical form back.
- If the drawing proves a localized OCSR error, keep the raw `molparser_esmi` and use the explicit `ocsr_override` lane.
- Preserve an unknown or unsupported tag and report the limitation. Never guess a translation.

These rules matter because extension positions are bound to the original non-canonical SMILES traversal. A chemically equivalent reordered SMILES can make the extension point at the wrong atom.

## Overall form

```text
SMILES<sep>EXTENSION
```

- The body is RDKit-compatible SMILES.
- `<sep>` separates the molecular graph from optional structured metadata.
- MolParser generates its body with a fixed rooted, non-canonical, isomeric traversal. Treat that traversal as part of the protocol.
- A normal concrete molecule may end in an empty `<sep>`.

Example:

```text
*c1ccccc1<sep><a>0:R[1]</a>
```

## Position semantics

In an atom-position tag, `ATOM_INDEX` is the atom position in the original SMILES output order, starting at zero. It is not an arbitrary RDKit atom index and must not be recomputed after canonicalization.

For example:

```text
<a>0:R[1]</a>
```

means that position 0 in the original body carries the displayed group or open Markush label `R[1]`.

## Extended-SMILES 2.0 tokens

| Token | Meaning in the supplied 2.0 definition | Extraction behavior |
|---|---|---|
| `<a>ATOM_INDEX:GROUP_NAME</a>` | Substituent, abbreviation, or open Markush group | Preserve label and position exactly. Examples include `R`, `R[1]`, `Ar`, `Ph`, `Me`, `OMe`, and `<id>[DNA]`. |
| `<d>ATOM_INDEX:ATTACH_POINT_LABEL</d>` | Explicit attachment/dummy point | Preserve; let the E-SMILES/FHR loader interpret it. |
| `<r>RING_INDEX:GROUP_NAME</r>` | Position-variable substituent on a ring | `RING_INDEX` is a separate ring index, not an atom index. Preserve ambiguity rather than choosing a ring atom. |
| `<c>ATOM_INDEX:CIRCLE_NAME</c>` | Abstract/simple circle or abstract ring marker | Preserve as an abstract feature; do not materialize a concrete ring without evidence. |
| `<g>[INNER:OUTER]:...:|Sg:n|</g>` | Repeating fragment with port pairs and repeat count | Preserve ports and count/range. Do not expand an abstract repeat unless a tool returns a validated concrete result. |
| `<v>VIRTUALARC_INDEX:NAME:[FROM:TO]</v>` | Virtual/abstract arc and its two endpoints | Endpoint order is undirected. Preserve it as abstract topology. |
| `<s>Extended-SMILES</s>` | Nested complex substituent in the 2.0 definition | Treat the nested value as its own E-SMILES coordinate space, starting at atom position zero. See runtime caveat below. |
| `|Sg:n|` | Whole polymer/repeating unit | Preserve the repeat annotation. |
| `?n`, `?m-n`, `?3` | Abstract, ranged, or fixed repetition attached to a group label | Preserve the repeat suffix; only a deterministic tool may resolve it. |

Labels may contain bracketed super/subscript-like identifiers such as `R[3]`. Do not normalize them to `R3`.

## Concrete, abbreviation, and open Markush labels

A `*` plus `<a>` does not always mean an unresolved variable:

- `R`, `R[n]`, `Ar`, `X`, and similar labels normally denote open Markush slots.
- `Me`, `Ph`, `OMe`, `CF3`, and known abbreviations may denote concrete substituents.
- `<id>[RNA]`, `<id>[DNA]`, and other configured identifiers denote intentionally abstract components.

Do not decide by punctuation alone. Let the vendored E-SMILES ingress and FHR tools classify or expand supported labels. In the extraction plan, preserve the original `esmi` regardless of that computational interpretation.

## Rings, nested structures, and repeats

- `<r>` position variation means the drawing does not specify one exact attachment atom. Do not invent regiochemistry.
- A nested substructure has its own atom-position numbering. Never apply its positions to the outer body.
- A repeat count may be exact, symbolic, or ranged. Do not turn `n` or `1-3` into one arbitrary concrete count.
- Product and reactant templates may remain abstract; concrete examples must be materialized only when image evidence or a validated deterministic roundtrip supports them.

## Runtime support boundary

The supplied PDF describes the intended Extended-SMILES 2.0 wire format. The vendored runtime currently parses the production subset through `third_party/molecular_assembly_v2/src/esmiles/`, including atom anchors, ring-position tags, dummy points, virtual arcs, S-groups/repeats, and supported abbreviation/repeat paths.

There is a version-sensitive nested-substructure difference:

- the supplied 2.0 document uses `<s>...</s>` for a nested substructure;
- parts of the vendored legacy/DP-aligned loader also recognize historical nested `<r>...</r>` blocks while distinguishing numeric `<r>RING_INDEX:LABEL</r>` ring-position tags.

Therefore:

1. preserve whichever syntax MolParser emitted;
2. do not translate `<s>` to `<r>` or `<r>` to `<s>` in the model;
3. if reverse/fill/render reports unsupported syntax, retain the raw template and record a limitation instead of hand-editing it;
4. only a tested parser migration may change the wire form.

## Explicitly unsupported or unresolved in the supplied definition

The 2.0 document lists these as not representable or not fully supported:

- coordination bonds;
- electron-transfer/mechanism arrows;
- uncertain/dashed/wavy bonds;
- unspecified chirality;
- multicenter bonds such as some boranes.

Do not force these into ordinary bonds or definite stereochemistry. Keep the drawing evidence, mark the ambiguity/limitation, and fail closed where the output contract requires a concrete structure.

## Source

Distilled from the owner-supplied 16-page document the owner-supplied MolParser format document, dated 2026-07-28. The PDF itself is not bundled so the canonical Skill stays lightweight and searchable.
