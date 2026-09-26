# RxnMiner figure runtime

The local Node service manages image uploads, Pi conversations, chemistry tools, immutable events,
validated V2 results, and the sealed `demo-figure` replay. The CLI starts it together with the
Python workspace and the shared Figure Reviewer.

Use the repository-root `npm ci`, `node ./rxnminer.mjs setup`, and `node ./rxnminer.mjs demo demo-figure` commands.
For live extraction, supply a vision-capable model account and UniParser credentials as described in
[getting started](../../../docs/getting-started.md).

Uploaded images are isolated by user and copied into each attempt's workspace. The runtime loads
pinned figure skills, verifies the chemistry-tool contract, and preserves source hashes. Cancellation,
retry, persistent sessions, and proposed human corrections use the same event store.

Run `npm test` here for the runtime tests, or `npm run build` to rebuild the service. PDF inputs,
page parsing, document skills, and PDF replay fixtures are not included.
