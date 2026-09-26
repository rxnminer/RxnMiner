"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

function canonicalPath(value) {
  const resolved = path.resolve(value);
  let existing = resolved;
  while (!fs.existsSync(existing)) {
    const parent = path.dirname(existing);
    if (parent === existing) break;
    existing = parent;
  }
  const canonicalExisting = fs.realpathSync(existing);
  return path.join(canonicalExisting, path.relative(existing, resolved));
}

function pathKey(value) {
  return ["darwin", "win32"].includes(process.platform)
    ? value.toLocaleLowerCase("en-US")
    : value;
}

function sameExistingLocation(left, right) {
  try {
    const leftStat = fs.statSync(left);
    const rightStat = fs.statSync(right);
    return leftStat.dev === rightStat.dev && leftStat.ino === rightStat.ino;
  } catch {
    return false;
  }
}

function isAtOrInside(value, parent) {
  const childKey = pathKey(value);
  const parentKey = pathKey(parent);
  if (childKey === parentKey || childKey.startsWith(`${parentKey}${path.sep}`)) {
    return true;
  }
  let candidate = value;
  while (!fs.existsSync(candidate)) {
    const next = path.dirname(candidate);
    if (next === candidate) break;
    candidate = next;
  }
  while (fs.existsSync(candidate)) {
    if (sameExistingLocation(candidate, parent)) return true;
    const next = path.dirname(candidate);
    if (next === candidate) break;
    candidate = next;
  }
  return false;
}

function sameLocation(left, right) {
  return isAtOrInside(left, right) && isAtOrInside(right, left);
}

function rejectOutputOverride(argv = process.argv) {
  if (argv.some((argument) =>
    argument === "--output" || argument.startsWith("--output="))) {
    throw new Error(
      "Playwright --output is disabled; use guarded PDF_REVIEWER_EVIDENCE instead",
    );
  }
}

function evidenceOutputDir(env = process.env) {
  const requestedParent = env.PDF_REVIEWER_EVIDENCE;
  if (requestedParent && !path.isAbsolute(requestedParent)) {
    throw new Error("PDF_REVIEWER_EVIDENCE must be an absolute external parent directory");
  }
  if (requestedParent) {
    let info;
    try {
      info = fs.lstatSync(requestedParent);
    } catch {
      throw new Error(
        "PDF_REVIEWER_EVIDENCE must already exist as a real external directory",
      );
    }
    if (info.isSymbolicLink() || !info.isDirectory()) {
      throw new Error(
        "PDF_REVIEWER_EVIDENCE must already exist as a real external directory",
      );
    }
    if (
      typeof process.getuid === "function"
      && typeof info.uid === "number"
      && info.uid !== process.getuid()
    ) {
      throw new Error("PDF_REVIEWER_EVIDENCE must be owned by the current user");
    }
    if ((info.mode & 0o022) !== 0) {
      throw new Error(
        "PDF_REVIEWER_EVIDENCE must not be group- or world-writable",
      );
    }
  }
  const parent = requestedParent
    ? canonicalPath(requestedParent)
    : fs.mkdtempSync(
      path.join(fs.realpathSync(os.tmpdir()), "chemrxn-pdf-evidence-parent-"),
    );
  const broadRoots = [
    canonicalPath(path.parse(parent).root),
    canonicalPath(os.homedir()),
    canonicalPath(os.tmpdir()),
    canonicalPath(path.resolve(__dirname, "../../..")),
  ];
  if (requestedParent && broadRoots.some((root) => sameLocation(root, parent))) {
    throw new Error(
      "PDF_REVIEWER_EVIDENCE must not be a filesystem, home, temp, or Git root",
    );
  }
  const repo = broadRoots.at(-1);
  if (requestedParent && isAtOrInside(parent, repo)) {
    throw new Error("PDF_REVIEWER_EVIDENCE must remain outside the Git checkout");
  }
  return fs.mkdtempSync(
    path.join(parent, "chemrxn-pdf-reviewer-test-results-"),
  );
}

module.exports = {
  evidenceOutputDir,
  rejectOutputOverride,
};
