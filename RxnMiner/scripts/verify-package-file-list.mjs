#!/usr/bin/env node

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const REQUIRED_PACKAGE_LICENSE_METADATA = [
  "harness/product_samples/README.md",
  "reviewer/vendor/ketcher/static/js/736.1a10862c.chunk.js.LICENSE.txt",
  "reviewer/vendor/ketcher/static/js/closable.bf629050.js.LICENSE.txt",
  "reviewer/vendor/ketcher/static/js/duo.b6f16b2b.js.LICENSE.txt",
  "reviewer/vendor/ketcher/static/js/main.00ee8faf.js.LICENSE.txt",
  "reviewer/vendor/ketcher/static/js/popup.41bce24d.js.LICENSE.txt",
];

export const REQUIRED_AGENT_SETUP_FILES = [
  "setup",
  "scripts/setup-agent-skills.mjs",
  "figure_skills/ATTRIBUTION.md",
  "figure_skills/pi-extension/index.ts",
  "figure_skills/scripts/run_chem_mcp.py",
  "figure_skills/skills/reaction-extract/SKILL.md",
  "figure_skills/skills/reaction-verify/SKILL.md",
  "figure_skills/tools/chem_mcp.py",
];

// The installed runtime verifies these canonical Product/Pi sources before it
// advertises extraction readiness. Keep them in the private package so the
// same fail-closed protocol gate works in a clone and in the packed product.
export const REQUIRED_RUNTIME_PROTOCOL_FILES = [
  "runtime/pi-service/package-lock.json",
  "runtime/pi-service/src/workspace-ui-tools.ts",
  "runtime/pi-service/src/chem-mcp-client.ts",
  "runtime/pi-service/src/pi-driver.ts",
  "runtime/pi-service/src/skills.ts",
  "runtime/pi-service/src/local-agent.ts",
];

const DENIED_PACKAGE_PATHS = [
  {
    label: "ground-truth payload",
    pattern: /(?:^|\/)(?:gt|ground[_-]?truth)(?:$|\/|[._-])/i,
  },
  {
    label: "benchmark, curation, or cached perception data",
    pattern: /^(?:benchmark|curation|pdf\/data\/(?:gt(?:_accuracy\d+)?|perception))(?:\/|$)/i,
  },
  {
    label: "credential or secret material",
    pattern: /(?:^|\/)(?:\.env(?:\.[^/]*)?|\.npmrc|\.pypirc|\.netrc|auth\.json|credentials?(?:\.[^/]*)?|secrets?(?:\.[^/]*)?|pi-auth)(?:$|\/)/i,
  },
  {
    label: "private data/gallery material",
    pattern: /(?:^|\/)(?:private(?:[_-](?:data|samples?|gallery))?|product-samples)(?:$|\/)/i,
  },
  {
    label: "private key material",
    pattern: /\.(?:key|pem|p12|pfx)$/i,
  },
];

export function verifyPackageFileList(packResult, packageJson) {
  const entry = Array.isArray(packResult) ? packResult[0] : packResult;
  const files = Array.isArray(entry?.files)
    ? entry.files.map((file) => file?.path).filter((value) => typeof value === "string")
    : [];
  const failures = [];

  if (!files.length) failures.push("npm pack result contains no file list");
  for (const packagedPath of files) {
    if (packagedPath.includes("\\") || path.posix.normalize(packagedPath) !== packagedPath) {
      failures.push(`unsafe or non-normalized packaged path: ${packagedPath}`);
      continue;
    }
    if (packagedPath === ".env.example") continue;
    const componentPath = packagedPath.replace(/^RxnMiner\//, "");
    for (const rule of DENIED_PACKAGE_PATHS) {
      if (rule.pattern.test(componentPath)) {
        failures.push(`${rule.label} is forbidden in the package: ${packagedPath}`);
      }
    }
  }

  if (packageJson?.private !== true || packageJson?.license !== "UNLICENSED") {
    failures.push("root package metadata must remain private:true and license:UNLICENSED");
  }
  const fileSet = new Set(files.map(file => file.replace(/^RxnMiner\//, "")));
  for (const requiredPath of REQUIRED_PACKAGE_LICENSE_METADATA) {
    if (!fileSet.has(requiredPath)) failures.push(`missing scoped license/attribution metadata: ${requiredPath}`);
  }
  for (const requiredPath of REQUIRED_AGENT_SETUP_FILES) {
    if (!fileSet.has(requiredPath)) failures.push(`missing universal Agent setup/runtime file: ${requiredPath}`);
  }
  for (const requiredPath of REQUIRED_RUNTIME_PROTOCOL_FILES) {
    if (!fileSet.has(requiredPath)) failures.push(`missing installed protocol gate file: ${requiredPath}`);
  }

  return { ok: failures.length === 0, fileCount: files.length, failures };
}

function main(argv) {
  const [packJsonPath, packageJsonPath = "package.json"] = argv;
  if (!packJsonPath) {
    process.stderr.write("Usage: verify-package-file-list.mjs PACK_JSON [PACKAGE_JSON]\n");
    return 2;
  }
  try {
    const report = verifyPackageFileList(
      JSON.parse(readFileSync(packJsonPath, "utf8")),
      JSON.parse(readFileSync(packageJsonPath, "utf8")),
    );
    if (!report.ok) {
      process.stderr.write(`ChemRxn package file-list policy failed:\n${report.failures.map((item) => `- ${item}`).join("\n")}\n`);
      return 1;
    }
    process.stdout.write(`ChemRxn package file-list policy verified (${report.fileCount} files).\n`);
    return 0;
  } catch (error) {
    process.stderr.write(`ChemRxn package file-list policy failed: ${error instanceof Error ? error.message : String(error)}\n`);
    return 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main(process.argv.slice(2));
}
