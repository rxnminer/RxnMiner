import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { loadPinnedSkillManifest } from "../src/skills.js";

test("skill loading verifies committed expected hashes instead of only reporting inventory", async () => {
  const repoRoot = fileURLToPath(new URL("../../../..", import.meta.url));
  const manifest = await loadPinnedSkillManifest(repoRoot);
  assert.deepEqual(manifest.map(({ name, sha256 }) => ({ name, sha256 })), [
    {
      name: "reaction-extract",
      sha256: "fb6741c353ad837169821f8751abdf2cbc56f90747ee007dafebb11e985b25c4",
    },
  ]);
  for (const skill of manifest) {
    const source = readFileSync(skill.path, "utf8");
    const frontmatterName = source.match(/^---\s*[\r\n]+name:\s*([^\r\n]+)/)?.[1]?.trim();
    assert.equal(frontmatterName, skill.name, `${skill.path} must be pinned under its frontmatter name`);
  }

  const altered = mkdtempSync(path.join(tmpdir(), "chemrxn-skills-altered-"));
  try {
    mkdirSync(path.join(altered, "figure_skills/skills/reaction-extract/references"), { recursive: true });
    writeFileSync(path.join(altered, "figure_skills/skills/reaction-extract/SKILL.md"), "altered");
    writeFileSync(path.join(altered, "figure_skills/skills/reaction-extract/LESSONS.md"), "altered");
    writeFileSync(
      path.join(altered, "figure_skills/skills/reaction-extract/references/extended-smiles-2.0.md"),
      "altered",
    );
    await assert.rejects(loadPinnedSkillManifest(altered), /Pinned skill (?:support-file )?hash mismatch/);
  } finally {
    rmSync(altered, { recursive: true, force: true });
  }

  const alteredReference = mkdtempSync(path.join(tmpdir(), "chemrxn-skill-reference-altered-"));
  try {
    mkdirSync(
      path.join(alteredReference, "figure_skills/skills/reaction-extract/references"),
      { recursive: true },
    );
    writeFileSync(
      path.join(alteredReference, "figure_skills/skills/reaction-extract/SKILL.md"),
      readFileSync(path.join(repoRoot, "figure_skills/skills/reaction-extract/SKILL.md")),
    );
    writeFileSync(
      path.join(alteredReference, "figure_skills/skills/reaction-extract/LESSONS.md"),
      readFileSync(path.join(repoRoot, "figure_skills/skills/reaction-extract/LESSONS.md")),
    );
    writeFileSync(
      path.join(
        alteredReference,
        "figure_skills/skills/reaction-extract/references/extended-smiles-2.0.md",
      ),
      "altered reference",
    );
    await assert.rejects(
      loadPinnedSkillManifest(alteredReference),
      /Pinned skill support-file hash mismatch/,
    );
  } finally {
    rmSync(alteredReference, { recursive: true, force: true });
  }

  const alteredLessons = mkdtempSync(path.join(tmpdir(), "chemrxn-skill-lessons-altered-"));
  try {
    mkdirSync(
      path.join(alteredLessons, "figure_skills/skills/reaction-extract/references"),
      { recursive: true },
    );
    writeFileSync(
      path.join(alteredLessons, "figure_skills/skills/reaction-extract/SKILL.md"),
      readFileSync(path.join(repoRoot, "figure_skills/skills/reaction-extract/SKILL.md")),
    );
    writeFileSync(
      path.join(alteredLessons, "figure_skills/skills/reaction-extract/LESSONS.md"),
      "altered lessons",
    );
    writeFileSync(
      path.join(
        alteredLessons,
        "figure_skills/skills/reaction-extract/references/extended-smiles-2.0.md",
      ),
      readFileSync(
        path.join(
          repoRoot,
          "figure_skills/skills/reaction-extract/references/extended-smiles-2.0.md",
        ),
      ),
    );
    await assert.rejects(
      loadPinnedSkillManifest(alteredLessons),
      /Pinned skill support-file hash mismatch/,
    );
  } finally {
    rmSync(alteredLessons, { recursive: true, force: true });
  }
});
