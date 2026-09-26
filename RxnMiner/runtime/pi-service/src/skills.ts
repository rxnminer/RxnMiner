import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { contentHash } from "./security.js";

export const PINNED_TOOL_ALLOWLIST = [
  "detect",
  "ocsr_detect",
  "assemble",
  "verify_smiles",
  "render_reactions",
  "reverse_rgroup",
  "fill_rgroup",
  "reverse_assemble",
] as const;

const EXPECTED_SKILL_SHA256 = {
  "reaction-extract": "fb6741c353ad837169821f8751abdf2cbc56f90747ee007dafebb11e985b25c4",
} as const;

const EXPECTED_SKILL_SUPPORT_SHA256 = {
  "figure_skills/skills/reaction-extract/LESSONS.md":
    "ff29c2f0b5e23d74e9b8e83be5b0420745441dcb3f28c3cdb1af6db56d485447",
  "figure_skills/skills/reaction-extract/references/extended-smiles-2.0.md":
    "f149eb402de4bfe5dd2b0f4263d29f417f921f17c525a79b8f000ec984356daf",
} as const;

export interface SkillManifestEntry {
  name: string;
  path: string;
  sha256: string;
}

export async function loadPinnedSkillManifest(repoRoot: string): Promise<SkillManifestEntry[]> {
  await Promise.all(
    Object.entries(EXPECTED_SKILL_SUPPORT_SHA256).map(async ([relativePath, expected]) => {
      const sha256 = contentHash(await readFile(resolve(repoRoot, relativePath)));
      if (sha256 !== expected) {
        throw new Error(`Pinned skill support-file hash mismatch: ${relativePath}`);
      }
    }),
  );

  const definitions = [
    ["reaction-extract", "figure_skills/skills/reaction-extract/SKILL.md"],
  ] as const;
  return Promise.all(
    definitions.map(async ([name, relativePath]) => {
      const path = resolve(repoRoot, relativePath);
      const sha256 = contentHash(await readFile(path));
      if (sha256 !== EXPECTED_SKILL_SHA256[name]) {
        throw new Error(`Pinned skill hash mismatch: ${name}`);
      }
      return { name, path, sha256 };
    }),
  );
}
