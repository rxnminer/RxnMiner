#!/usr/bin/env node

import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  formatInstallIntegrityFailures,
  verifyInstallIntegrity,
} from "../cli/src/install-integrity.mjs";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const report = await verifyInstallIntegrity(repo);

if (!report.ok) {
  process.stderr.write(`${formatInstallIntegrityFailures(report)}\n`);
  process.exitCode = 1;
} else {
  // npm routes lifecycle banners to stderr in --json mode, but direct script
  // stdout would corrupt npm pack/publish's machine-readable JSON payload.
  const output = process.env.npm_config_json === "true" ? process.stderr : process.stdout;
  output.write(`${formatInstallIntegrityFailures(report)}\n`);
}
