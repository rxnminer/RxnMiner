import { createHash } from "node:crypto";
import { lstat, readFile, realpath } from "node:fs/promises";
import path from "node:path";

export const INSTALL_INTEGRITY_SCHEMA = "chemrxn.install-integrity.v1";
export const PACKAGE_PREPARE_LIFECYCLE = "npm run package:build";
export const INSTALL_INTEGRITY_MANIFEST = "packaging/install-integrity.json";
export const GIT_LFS_POINTER_HEADER = "version https://git-lfs.github.com/spec/v1";

const SHA256_PATTERN = /^[a-f0-9]{64}$/;

export async function verifyInstallIntegrity(repo, options = {}) {
  let root;
  try {
    root = await realpath(path.resolve(repo));
  } catch (error) {
    return failedReport("package-root", String(repo), errorMessage(error), [
      installCheck(
        "package-root-containment",
        false,
        `missing or unreadable package root: ${String(repo)}`,
        "Reinstall from the pinned release into a readable local package directory.",
      ),
    ]);
  }
  let manifest = options.manifest;
  if (!manifest) {
    try {
      manifest = JSON.parse(await readFile(
        await resolveVerifiedPackagePath(root, INSTALL_INTEGRITY_MANIFEST),
        "utf8",
      ));
    } catch (error) {
      return failedReport("manifest", INSTALL_INTEGRITY_MANIFEST, errorMessage(error), [
        installCheck(
          "install-integrity-manifest",
          false,
          `missing or invalid: ${INSTALL_INTEGRITY_MANIFEST}`,
          `Restore ${INSTALL_INTEGRITY_MANIFEST} from the pinned release.`,
        ),
      ]);
    }
  }

  const manifestErrors = validateManifest(manifest);
  if (manifestErrors.length) {
    return {
      ...emptyReport(manifest?.schemaVersion),
      ok: false,
      checks: [installCheck(
        "install-integrity-manifest",
        false,
        manifestErrors.join("; "),
        `Restore ${INSTALL_INTEGRITY_MANIFEST} from the pinned release.`,
      )],
      failures: manifestErrors.map((detail) => ({
        code: "INVALID_MANIFEST",
        path: INSTALL_INTEGRITY_MANIFEST,
        detail,
      })),
    };
  }

  const [packageContract, requiredFiles, assets] = await Promise.all([
    inspectPackageContract(root),
    Promise.all(manifest.requiredFiles.map((relativePath) => inspectRequiredFile(root, relativePath))),
    Promise.all(manifest.assets.map((asset) => inspectAsset(root, asset))),
  ]);
  const lfsAssets = assets.filter((asset) => asset.kind.startsWith("git-lfs-"));
  const sampleAssets = assets.filter((asset) => asset.kind === "source-only-sample");
  const hashFailures = assets.filter((asset) => !asset.hashMatches);
  const pointerFailures = lfsAssets.filter((asset) => asset.lfsPointer);
  const sampleFailures = sampleAssets.filter((asset) => !asset.ok);
  const missingRequired = requiredFiles.filter((file) => !file.ok);
  const failures = [
    ...packageContract.failures,
    ...missingRequired.map((file) => ({
      code: file.unsafePath
        ? "UNSAFE_PACKAGE_PATH"
        : file.lfsPointer
          ? "GIT_LFS_POINTER"
          : "REQUIRED_FILE_INVALID",
      path: file.path,
      detail: file.detail,
    })),
    ...assets.filter((asset) => !asset.ok).map((asset) => ({
      code: asset.unsafePath
        ? "UNSAFE_PACKAGE_PATH"
        : asset.lfsPointer
          ? "GIT_LFS_POINTER"
          : asset.exists
            ? "SHA256_MISMATCH"
            : "ASSET_MISSING",
      path: asset.path,
      detail: asset.detail,
      ...(asset.expectedSha256 ? { expectedSha256: asset.expectedSha256 } : {}),
      ...(asset.actualSha256 ? { actualSha256: asset.actualSha256 } : {}),
    })),
  ];
  const checks = [
    installCheck(
      "private-package-contract",
      packageContract.ok,
      packageContract.ok
        ? "private:true · prepare-only offline build · UNLICENSED root · no prepack/postinstall"
        : packageContract.failures.map((failure) => failure.detail).join("; "),
      "Restore package.json from the pinned release; ChemRxn must stay private and use its single offline prepare build.",
    ),
    installCheck(
      "packaged-runtime",
      missingRequired.length === 0,
      missingRequired.length === 0
        ? `${requiredFiles.length}/${requiredFiles.length} required package files present`
        : missingRequired.map((file) => `${file.path}: ${file.detail}`).join("; "),
      "Reinstall from the pinned Git release so npm can run the explicit prepare build.",
    ),
    installCheck(
      "packaged-asset-sha256",
      hashFailures.length === 0,
      hashFailures.length === 0
        ? `${assets.length}/${assets.length} packaged assets match SHA-256`
        : hashFailures.map((asset) => `${asset.path}: ${asset.detail}`).join("; "),
      "Reinstall from the pinned release; do not use an asset whose SHA-256 differs from the release manifest.",
    ),
    installCheck(
      "git-lfs-materialization",
      pointerFailures.length === 0 && lfsAssets.every((asset) => asset.ok),
      pointerFailures.length
        ? `Git LFS pointer files found: ${pointerFailures.map((asset) => asset.path).join(", ")}`
        : lfsAssets.every((asset) => asset.ok)
          ? `${lfsAssets.length}/${lfsAssets.length} Ketcher/RDKit Git LFS assets materialized`
          : "one or more Ketcher/RDKit Git LFS assets are missing or invalid",
      "Install Git LFS, run `git lfs install`, then retry the pinned Git install (or run `git lfs pull` in a checkout).",
    ),
    installCheck(
      "bundled-source-samples",
      sampleFailures.length === 0,
      sampleFailures.length === 0
        ? `${sampleAssets.length}/${sampleAssets.length} source-only sample files match SHA-256`
        : sampleFailures.map((asset) => `${asset.path}: ${asset.detail}`).join("; "),
      "Restore the attributed bundled samples from the pinned release.",
    ),
  ];

  return {
    ok: failures.length === 0,
    schemaVersion: manifest.schemaVersion,
    manifest: INSTALL_INTEGRITY_MANIFEST,
    assetCount: assets.length,
    verifiedAssetCount: assets.filter((asset) => asset.ok).length,
    lfsAssetCount: lfsAssets.length,
    sampleAssetCount: sampleAssets.length,
    requiredFileCount: requiredFiles.length,
    checks,
    failures,
    assets,
  };
}

export function isGitLfsPointer(value) {
  const prefix = Buffer.isBuffer(value)
    ? value.subarray(0, Buffer.byteLength(GIT_LFS_POINTER_HEADER)).toString("utf8")
    : String(value).slice(0, GIT_LFS_POINTER_HEADER.length);
  return prefix === GIT_LFS_POINTER_HEADER;
}

export function formatInstallIntegrityFailures(report) {
  if (report.ok) {
    return `ChemRxn install integrity verified (${report.verifiedAssetCount}/${report.assetCount} assets).`;
  }
  const lines = ["ChemRxn install integrity failed:"];
  for (const failure of report.failures) {
    lines.push(`- ${failure.code} ${failure.path}: ${failure.detail}`);
  }
  if (report.failures.some((failure) => failure.code === "GIT_LFS_POINTER")) {
    lines.push("Install Git LFS, run `git lfs install`, and retry the pinned Git install.");
  }
  return lines.join("\n");
}

async function inspectPackageContract(root) {
  const failures = [];
  let packageJson;
  try {
    const packagePath = await resolveVerifiedPackagePath(root, "package.json");
    packageJson = JSON.parse(await readFile(packagePath, "utf8"));
  } catch (error) {
    return {
      ok: false,
      failures: [{
        code: isUnsafePackagePathError(error) ? "UNSAFE_PACKAGE_PATH" : "PACKAGE_JSON_INVALID",
        path: "package.json",
        detail: errorMessage(error),
      }],
    };
  }
  const requireContract = (condition, detail) => {
    if (!condition) failures.push({ code: "PACKAGE_CONTRACT", path: "package.json", detail });
  };
  requireContract(packageJson.name === "@rxnminer/workbench", "package name must remain @rxnminer/workbench");
  requireContract(packageJson.private === true, "private:true is required; registry publication is out of scope");
  requireContract(
    packageJson.license === "UNLICENSED",
    "license must be UNLICENSED until the repository has a root-wide license grant",
  );
  requireContract(packageJson.bin?.chemrxn === "cli/bin/chemrxn.mjs", "chemrxn bin entry is missing");
  requireContract(
    packageJson.scripts?.prepare === PACKAGE_PREPARE_LIFECYCLE,
    `prepare must be ${JSON.stringify(PACKAGE_PREPARE_LIFECYCLE)}`,
  );
  requireContract(!packageJson.scripts?.prepack, "prepack must stay absent; npm pack already invokes prepare");
  requireContract(!packageJson.scripts?.postinstall, "postinstall must stay absent; setup is explicit");
  for (const packagedPath of [
    "packaging/install-integrity.json",
    "scripts/verify-install-integrity.mjs",
    "runtime/pi-service/dist/src",
  ]) {
    requireContract(
      packageJson.files?.includes(packagedPath),
      `files allowlist must include ${packagedPath}`,
    );
  }
  return { ok: failures.length === 0, failures };
}

async function inspectRequiredFile(root, relativePath) {
  try {
    const absolutePath = await resolveVerifiedPackagePath(root, relativePath);
    const info = await lstat(absolutePath);
    if (!info.isFile() || info.isSymbolicLink()) {
      return { path: relativePath, ok: false, detail: "not a regular package file", lfsPointer: false };
    }
    if (info.size === 0) {
      return { path: relativePath, ok: false, detail: "empty package file", lfsPointer: false };
    }
    const bytes = await readFile(absolutePath);
    const lfsPointer = isGitLfsPointer(bytes);
    return {
      path: relativePath,
      ok: !lfsPointer,
      detail: lfsPointer ? "unmaterialized Git LFS pointer" : `${info.size} bytes`,
      lfsPointer,
    };
  } catch (error) {
    return {
      path: relativePath,
      ok: false,
      detail: errorMessage(error),
      lfsPointer: false,
      unsafePath: isUnsafePackagePathError(error),
    };
  }
}

async function inspectAsset(root, asset) {
  try {
    const absolutePath = await resolveVerifiedPackagePath(root, asset.path);
    const info = await lstat(absolutePath);
    if (!info.isFile() || info.isSymbolicLink()) {
      return invalidAsset(asset, "not a regular package file");
    }
    const bytes = await readFile(absolutePath);
    const lfsPointer = isGitLfsPointer(bytes);
    const actualSha256 = sha256(bytes);
    const hashMatches = actualSha256 === asset.sha256;
    return {
      path: asset.path,
      kind: asset.kind,
      ok: !lfsPointer && hashMatches,
      exists: true,
      lfsPointer,
      hashMatches,
      expectedSha256: asset.sha256,
      actualSha256,
      bytes: info.size,
      detail: lfsPointer
        ? "unmaterialized Git LFS pointer"
        : hashMatches
          ? `${info.size} bytes · sha256:${actualSha256}`
          : `SHA-256 mismatch (expected ${asset.sha256}, got ${actualSha256})`,
    };
  } catch (error) {
    return invalidAsset(asset, errorMessage(error), isUnsafePackagePathError(error));
  }
}

function invalidAsset(asset, detail, unsafePath = false) {
  return {
    path: asset.path,
    kind: asset.kind,
    ok: false,
    exists: false,
    lfsPointer: false,
    hashMatches: false,
    expectedSha256: asset.sha256,
    detail,
    unsafePath,
  };
}

function validateManifest(manifest) {
  const errors = [];
  if (!manifest || typeof manifest !== "object") return ["manifest must be an object"];
  if (manifest.schemaVersion !== INSTALL_INTEGRITY_SCHEMA) {
    errors.push(`unsupported schemaVersion: ${String(manifest.schemaVersion)}`);
  }
  if (!Array.isArray(manifest.requiredFiles) || !manifest.requiredFiles.length) {
    errors.push("requiredFiles must be a non-empty array");
  }
  if (!Array.isArray(manifest.assets) || !manifest.assets.length) {
    errors.push("assets must be a non-empty array");
  }
  const paths = [];
  for (const relativePath of manifest.requiredFiles || []) {
    if (!validManifestPath(relativePath)) errors.push(`invalid required file path: ${String(relativePath)}`);
    paths.push(relativePath);
  }
  for (const asset of manifest.assets || []) {
    if (!asset || typeof asset !== "object") {
      errors.push("asset entries must be objects");
      continue;
    }
    if (!validManifestPath(asset.path)) errors.push(`invalid asset path: ${String(asset.path)}`);
    if (!SHA256_PATTERN.test(asset.sha256 || "")) errors.push(`invalid SHA-256 for ${String(asset.path)}`);
    if (!["git-lfs-ketcher", "git-lfs-rdkit", "source-only-sample"].includes(asset.kind)) {
      errors.push(`invalid asset kind for ${String(asset.path)}: ${String(asset.kind)}`);
    }
    paths.push(asset.path);
  }
  const duplicates = paths.filter((value, index) => paths.indexOf(value) !== index);
  if (duplicates.length) errors.push(`duplicate manifest paths: ${[...new Set(duplicates)].join(", ")}`);
  return errors;
}

function validManifestPath(value) {
  if (typeof value !== "string" || !value || value.includes("\\")) return false;
  const normalized = path.posix.normalize(value);
  return normalized === value && !normalized.startsWith("../") && !path.posix.isAbsolute(normalized);
}

async function resolveVerifiedPackagePath(root, relativePath) {
  if (!validManifestPath(relativePath)) {
    throw new UnsafePackagePathError(`Unsafe install-integrity path: ${relativePath}`);
  }

  let candidate = root;
  for (const component of relativePath.split("/")) {
    candidate = path.join(candidate, component);
    const info = await lstat(candidate);
    if (info.isSymbolicLink()) {
      throw new UnsafePackagePathError(
        `symbolic-link package path component rejected: ${path.relative(root, candidate)}`,
      );
    }
  }

  const resolved = await realpath(candidate);
  const relative = path.relative(root, resolved);
  if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new UnsafePackagePathError(
      `package path escapes root after realpath: ${relativePath}`,
    );
  }
  return resolved;
}

class UnsafePackagePathError extends Error {
  constructor(message) {
    super(message);
    this.name = "UnsafePackagePathError";
  }
}

function isUnsafePackagePathError(error) {
  return error instanceof UnsafePackagePathError;
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function installCheck(name, ok, detail, fix) {
  return { name, ok, required: true, detail, ...(ok ? {} : { fix }) };
}

function failedReport(code, failurePath, detail, checks) {
  return {
    ...emptyReport(),
    ok: false,
    checks,
    failures: [{ code: code.toUpperCase(), path: failurePath, detail }],
  };
}

function emptyReport(schemaVersion) {
  return {
    ok: false,
    schemaVersion: schemaVersion || null,
    manifest: INSTALL_INTEGRITY_MANIFEST,
    assetCount: 0,
    verifiedAssetCount: 0,
    lfsAssetCount: 0,
    sampleAssetCount: 0,
    requiredFileCount: 0,
    checks: [],
    failures: [],
    assets: [],
  };
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}
