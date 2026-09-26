import { spawnSync } from "node:child_process";

/**
 * Use an already prepared chemistry Python when one is available. Falling
 * back to python3 intentionally exercises the canonical runner's own
 * bootstrap path, but avoids reinstalling RDKit on every developer machine.
 */
export function chemistryTestPython(): string {
  const configured = process.env.CHEMRXN_CHEM_PYTHON;
  if (configured) return configured;
  for (const candidate of ["python", "python3"]) {
    const probe = spawnSync(candidate, ["-c", "import PIL, rdkit, requests"], {
      stdio: "ignore",
      timeout: 15_000,
    });
    if (probe.status === 0) return candidate;
  }
  return "python3";
}
