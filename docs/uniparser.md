# Molecule recognition with UniParser

RxnMiner uses the public [UniParser-Tools SDK](https://github.com/dptech-corp/UniParser-Tools),
pinned to commit `2ba65f71b3c9bdf62c7db413f87686211ad5fdeb` in the Python requirements.
Apply your own key from [uniparser.dp.tech](https://uniparser.dp.tech/):

```bash
export UNIPARSER_BASE_URL=https://uniparser.dp.tech/
export UNIPARSER_API_KEY=your-key
```

Set these variables in the shell that starts RxnMiner. `.env.example` documents the names
but is not loaded automatically. `UNIPARSER_HOST` remains an alias for older PDF setups;
`UNIPARSER_BASE_URL` takes precedence for molecule recognition.

The molecule adapter submits an image through `trigger_snip` with molecule recognition
enabled, text disabled, synchronous processing, and no image padding. It then requests
`pages_dict` and molecule sources through `get_result`.

Bounding boxes are mapped to the original image pixel frame. E-SMILES (`esmi`) and SMILES
(`smi`) remain byte-preserved. Human captions remain in `source_caption`; legacy consumers
receive the chemical string in `caption`. Failed recognition records are retained if they
have valid geometry, so the agent can inspect the source instead of losing an occurrence.

No internal recognition address or API key is bundled. Missing keys and SDK/service errors
produce a configuration or recognition error; service response bodies are not exposed in
the message. The existing hash-bound `OCSR_CACHE` path remains available for offline replay.

The independent agent contains the same adapter to remain self-contained in Docker. Its
copy is checked for byte equality by the release audit.

Public UniParser accounts support limited concurrency (currently at most five requests).
Keep concurrent extraction workers within your account allowance. Live service quality
depends on the selected UniParser deployment; archived benchmark scores used the original
recognition service and are not promised to be identical with this provider.
