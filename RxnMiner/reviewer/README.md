# Figure Reviewer

The Reviewer provides the visual portion of the RxnMiner image workflow: source images and
bounding boxes, molecular structures rendered with RDKit, reaction records, Ketcher editing,
and export. These components are required to reproduce the interactive figure demo.

Start the full application from the repository root:

```bash
node ./rxnminer.mjs start
node ./rxnminer.mjs demo demo-figure
```

Import an existing image extraction with:

```bash
node ./rxnminer.mjs review prediction.v2.json --image figure.png --perception molparser.json
```

Source images, original predictions, and human edits are stored separately in the external local
data directory. The compatibility setting `PDF_ROOT` names the Reviewer data directory; it does
not enable PDF extraction. Vendored Ketcher and RDKit files retain their original notices.
