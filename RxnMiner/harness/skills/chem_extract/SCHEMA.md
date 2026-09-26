# SCHEMA — what you produce (V2, the format the viewer renders)

You write a **plan.json**, `tool.py emit` turns it into the **V2 prediction**. Both are
below. `emit` only fills provenance/validation boilerplate — your job is the chemistry in
the plan.

---

## The plan you write (input to `emit`)

```jsonc
{
  "is_multistep": false,                  // true only for chained syntheses (sub-recipe D)
  "selected_examples": [                   // ⭐ one entry per CONCRETE reaction instance
    {
      "reactants": ["O=C(OC1CC1)c1ccccc1", "..."],   // CONCRETE SMILES (no '*'), from fill/perception
      "products":  ["CC(NC(=O)c1ccccc1)c1ccccc1"],
      "conditions":["PhI(OTf)2","Selectfluor","TEA","MeCN, 120 C"],  // verbatim arrow text (optional)
      "yield_text":"74%",                  // optional
      "label":"4l",                        // the compound/entry id from the figure (optional but useful)
      "failed":false,                      // true for red-✗ / n.r. / 0% / trace (then products may be [])
      "markush_enumeration":{"R":"Ph"}     // optional: record which R you substituted
    }
    // ... one per drawn example / table row / arrow
  ],
  "reaction_templates": [                   // optional: the GENERIC markush form (kept verbatim, with '*')
    {"reactants":["*C(OC1CC1)=O<sep><a>0:R</a>"], "products":["*C(NC(*)*)=O<sep><a>0:R</a><a>4:Me</a><a>5:Ph</a>"]}
  ],
  "reaction_graph": [                       // optional, multistep only — the DAG of arrows
    {"edge_id":"e1","step_order":1,"step_label":"Step 1","from_mols":["1"],"to_mols":["2"],"via_template":"ex1"}
  ]
}
```

**Rules that matter for scoring:**
- `selected_examples` is what the scorer counts. **A figure with drawn product examples
  must have one entry per example** — never emit an empty list, never leave a reactant as
  `*` (it scores 0). The product is the concrete drawn structure; the reactant is the
  generic one with this example's R substituted in.
- `reaction_templates` (the markush generic) is optional context; it is NOT scored as a
  concrete reaction (its `*` makes it un-matchable, which is correct). Include it for
  provenance, but the concrete `selected_examples` are what count.
- Conditions are NOT scored — but include them; the viewer shows them and they document
  the reaction. Copy verbatim.

---

## The V2 prediction (output of `emit`) — what the viewer reads

```jsonc
{
  "selected_examples": [
    {
      "instance_id": "4l",
      "reactants": [
        {"identifier":"r0","E-SMILES":"O=C(OC1CC1)c1ccccc1","_source":{"type":"image_structure","agent":"skill"}}
      ],
      "products": [
        {"identifier":"p0","E-SMILES":"CC(NC(=O)c1ccccc1)c1ccccc1","_source":{"type":"image_structure","agent":"skill"}}
      ],
      "conditions": [[{"role":"reagent","text":"MeCN, 120 C"}]],   // outer list = step, inner = entries
      "markush_enumeration": {"R":"Ph"},
      "metrics": [{"for_product":"p0","content":"74%"}],
      "_outcome": {"is_failed": false}                              // present only if failed
    }
  ],
  "reaction_templates": [ ... ],          // same shape, E-SMILES keep their '*'
  "reaction_graph": [ ... ],              // multistep edges
  "is_multistep": false,
  "_extractor": "chem_extract_skill"
}
```

Field meanings (the ones the viewer + scorer use):
- **`E-SMILES`** — the molecule. For a `selected_examples` molecule it is a fully concrete,
  RDKit-canonical SMILES (no `*`). `<sep>` may separate multiple components of one entry.
- **`reactants[] / products[]`** — the scored content; each a list of molecule entries.
- **`conditions`** — list-of-list: outer = reaction step, inner = condition entries
  `{role, text}`. roles: reagent / catalyst / base / solvent / temperature / time / light / …
- **`markush_enumeration`** — `{label: value}` recording the R you used for this instance.
- **`metrics`** — `[{for_product, content}]` for yields/activities (verbatim text).
- **`_outcome.is_failed`** — `true` for a failed step (red ✗ / n.r. / 0%); then `products`
  may be empty.
- **`reaction_graph`** (multistep) — edges `{edge_id, step_order, step_label, from_mols,
  to_mols, via_template}`. A failed step has `to_mols: []`.
- **`is_multistep`** — true if the figure is a chained synthesis.

You don't hand-build the output shape — `emit` does. You only get the chemistry right in
the plan, and make `emit` print `VERDICT: VALID`.

---

## Richer (optional) plan fields — for a FULL V2, not just a scorable one

Everything above is enough to SCORE (reactants/products are all the F1 needs). But the figure
carries more, and the viewer renders it. `emit` supports all of it; populate what's in the
figure (the CRITIQUE's `FIELD COVERAGE:` line shows what you filled). Any reactant/product/
condition may be a **plain string** (simple) OR a **dict with provenance** (faithful):

```jsonc
{
  "is_multistep": true,
  "synthesis_overview": {"type":"linear|fan_out_derivatization|mechanistic_branched|linear_with_failures",
                          "description":"verbatim what the scheme shows"},
  "selected_examples": [{
    "label":"3a",
    "reactants":[{"smiles":"...", "identifier":"1", "bbox":[x0,y0,x1,y1], "mol_id":"p1_1", "role":"reactant"}],
    "products": [{"smiles":"...", "identifier":"3a", "bbox":[...]}],
    "conditions":[{"role":"catalyst","text":"Pd(OAc)2","smiles":"[Pd]"}, "K2CO3", "DMF, 80 C, 12 h"],
    "metrics":{"yield":"82%","dr":"5:1","ee":"90%"},      // structured (not just yield)
    "description":"3a, 82% yield, 5:1 d.r.",              // verbatim transcript at this example
    "inherit_template":"T1",
    "_multistep":{"step_order":1,"sequence_id":"s1","intermediate_role":"start"}
  }],
  "reaction_templates":[{"template_id":"T1","reactants":["*...<a>0:R</a>"],"products":["*..."],"_multistep":{"step_order":1}}],
  "reaction_graph":[{"edge_id":"e1","step_order":1,"step_label":"Step I","from_mols":["1"],"to_mols":["3a"],"via_template":"T1"}]
}
```
- **provenance** → `_source` carries `bbox`/`mol_id` (link back to the figure). Use `perceive`'s bbox/idx.
- **drawn-reagent conditions** → a reagent drawn as a structure becomes a condition entry with `E-SMILES`.
- **metrics** → a dict `{yield,dr,ee,time,temp}` becomes structured `metrics[]`.
- **multistep** → `is_multistep` + `reaction_graph` (validated against your mol ids/labels) + `synthesis_overview`.
- `emit` warns if `is_multistep:true` but the graph is empty, or a graph edge references an unknown mol-id.

A copy-ready full-field skeleton is `templates/prediction.blank.v2.json`.
