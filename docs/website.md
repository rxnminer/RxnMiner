# Project website

The Astro site includes an English home page, one visual demo, and a benchmark table
with cohort, stereochemistry, and sorting controls.

```bash
npm --prefix website ci
npm --prefix website run check
npm --prefix website run build
npm --prefix website run preview
```

The visual demo and English captions are bundled in `website/public/media`.

For GitHub project Pages, build with the repository base path:

```bash
SITE_BASE=/RxnMiner npm --prefix website run build
```

The `pages.yml` workflow builds on pushes and pull requests and deploys `main` through
GitHub Pages after Pages has been enabled for the repository. Set the Pages source to
**GitHub Actions**. The resulting project site is
`https://<owner>.github.io/RxnMiner/`; the workflow supplies the `/RxnMiner` base path.

The planned URL is `https://rxnminer.github.io/RxnMiner/`. Until deployment has been enabled,
use the local preview or host the generated `website/dist` folder under the chosen base path.

Update metrics from a reviewed result snapshot and keep the cohort and scoring notes in
sync. Never replace a partially covered lane with a full-cohort claim.
