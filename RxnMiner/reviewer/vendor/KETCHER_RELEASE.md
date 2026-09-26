# Vendored Ketcher standalone

`reviewer/vendor/ketcher/` is the unpacked standalone browser release used by the Reviewer iframe.

- Source: `git@git.dp.tech:pdf-extract/ketcher.git`
- Ketcher commit: `b8d3cddb910948266d4ee7fae987fbc6fe0bc1d3`
- Source tags: `latest`, `ketcher-b8d3cddb`
- Indigo Ketcher: `1.44.8-9`
- Indigo commit: `f24a62cf`
- Release asset: `ketcher-standalone-b8d3cddb-indigo-1.44.8-9-f24a62cf.tar.gz`
- Release asset SHA-256: `5edb8fae589e28068e8560873de12cf13b3bd5d986ebad0b67dd59d170b54ed0`
- Imported: 2026-07-28
- Deployment trimming: build source maps (`*.map`) are omitted; runtime HTML, JS, CSS, licenses, RDKit WASM, and Indigo WASM are preserved.

This build contains the real MolParser E-SMILES importer/exporter. The supported iframe contract is:

```javascript
await iframe.contentWindow.ketcher.setMolecule(fullEsmiles);
const editedEsmiles = await iframe.contentWindow.ketcher.getEsmiles();
```

Do not replace `getEsmiles()` with `getSmiles()` and do not strip `<sep>` before loading. The immutable raw MolParser `ocsr_esmi` remains provenance; Reviewer edits are written to reviewed sidecars.

Files under `reviewer/vendor/ketcher/` are Git LFS managed. Update only from an immutable standalone release and refresh this provenance record and the iframe roundtrip tests in the same change.
