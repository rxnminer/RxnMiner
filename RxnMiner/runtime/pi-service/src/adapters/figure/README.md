# Figure job adapter contract

Status: proposed by `team:agent-fig` for review by `team:ui-product` in Issue #63.

This adapter maps canonical Figure-agent outputs into a replayable product contract. It does not run a model, call MolParser, render UI, mutate a raw prediction, or read reference data.

## Boundary

Input chemistry remains authoritative in the Figure pipeline:

```text
image + sibling molparser.json + Pi/Skill tool trace + raw V2 + validation
```

The adapter emits:

```text
FigureJobManifest + ordered FigureEvent[]
```

The common runtime owns session persistence and the cross-domain event envelope. This module owns Figure-specific payloads, stable chemistry links, honest progress boundaries, artifact reuse, and replay semantics.

## Stable identities

IDs are based on immutable input identity and semantic occurrence paths:

```text
fig:<input-sha-prefix>
det:<figure-id>:molparser:<bbox-index>
bbox:<figure-id>:figure-pixel:<bbox-index>
rxn:<figure-id>:<example|template>:<printed-id>:<duplicate-ordinal>
mol:<reaction-id>:<reactant|product|condition>:<ordinal>
evidence:<figure-id>:<kind>:<key>
artifact:<job-id>:<kind>:<sha-prefix>
```

A molecule ID identifies an occurrence, not a canonical structure. It therefore remains meaningful when two reactions contain the same SMILES. Printed identifiers are not assumed unique; the duplicate ordinal disambiguates them. The legacy V2 array index is retained for compatibility but is not the new public identity.

A derived molecule receives no fabricated bbox. It can be selected through its reaction and the reaction's drawn product or template evidence.

## Validation semantics

Connectivity and strict stereochemistry are separate fields.

- `connectivity=valid` means the designated chemistry validator parsed/accepted connectivity under the recorded basis.
- `strictStereo=assigned` means stereo is encoded; it does not mean that encoded stereo agrees with pixels or a post-hoc reference.
- Correctness comparisons and human adjudication are separate post-hoc evidence.
- OCSR confidence is never converted into correctness.

## Outcome classes

- `successful`: concrete reaction with a product and no unresolved required molecule.
- `failed`: explicitly marked failed/limitation outcome; an empty product alone is not enough.
- `template`: reaction template/Markush record.
- `unresolved`: required chemistry or identifier is missing or ambiguous.
- `partial`: explicitly accepted partial job/reaction diagnostic.

Dummy-bearing, non-roundtripping, or ambiguous reverse output must remain partial/unresolved rather than successful.

## Events and progress

Events carry monotonically increasing `seq`, stable `eventId`, and an `idempotencyKey`. Large content is referenced through artifact IDs and hashes.

The adapter emits numeric progress only when a real denominator exists:

- detections after MolParser returns or a cache is loaded;
- calls in a sealed tool sequence;
- reactions in the materialized V2;
- artifacts in the final manifest.

No wall-clock percentage or UI timer is part of this contract.

`molparser.cache_hit` and `molparser.started/completed` are mutually exclusive for one attempt. `cancel_requested` is not a terminal state; `cancelled` is.

## Retry and reuse

A retry creates a new `attemptId` under the same product job. Immutable content-addressed artifacts may be reused:

- input image;
- raw MolParser cache;
- completed validation/tool diagnostics;
- a V2 prediction only when it reached the declared valid terminal contract.

Incomplete model text or an unvalidated V2 file is diagnostic only. The new attempt gets new event IDs, and idempotency keys prevent duplicate events within one attempt.

## Replay fixture

`test/fixtures/figure/demo-figure/` contains a real sealed pre-reference prediction and cached MolParser payload. The original image remains external and is pinned by SHA-256. Replay:

1. verifies fixture hashes;
2. rejects path traversal and reference sentinels;
3. builds stable chemistry links;
4. emits deterministic ordered events;
5. performs no model, MolParser, or network call.

Post-hoc review/reference data is deliberately absent from the replay fixture.

## UI consumption

The existing UI should map these identities into `BBoxImagePanel`, `MoleculeCard`, `ReactionRow`, and `reaction-link.js`. This module does not define a second renderer. Any change to the common runtime event envelope or shared renderer is an interface request to `team:ui-product`.
