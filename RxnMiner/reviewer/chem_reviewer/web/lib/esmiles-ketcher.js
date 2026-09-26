const SEPARATOR = "<sep>";
const SUPPORTED_EXTENSION_TAGS = new Set(["a", "d", "r", "c", "v", "g", "s"]);
const INLINE_EXTENSION_TAGS = new Set(["dum", "id"]);

export function splitEsmiles(value) {
  const text = String(value || "").trim();
  const separator = text.indexOf(SEPARATOR);
  if (separator < 0) return { body: text, extension: "", hasSeparator: false };
  return {
    body: text.slice(0, separator).trim(),
    extension: text.slice(separator + SEPARATOR.length).trim(),
    hasSeparator: true,
  };
}

export function hasEsmilesExtension(value) {
  return Boolean(splitEsmiles(value).extension);
}

function extensionTagCounts(extension) {
  const counts = new Map();
  for (const match of extension.matchAll(/<([A-Za-z][A-Za-z0-9_-]*)\b[^>]*>/g)) {
    const tag = match[1].toLowerCase();
    if (tag === "sep") continue;
    counts.set(tag, (counts.get(tag) || 0) + 1);
  }
  return counts;
}

function extensionSemanticTokens(extension) {
  const tokens = [];
  for (const tag of ["a", "d", "r", "c", "v", "g", "s"]) {
    const pattern = new RegExp(`<${tag}\\b[^>]*>([\\s\\S]*?)<\\/${tag}\\s*>`, "gi");
    for (const match of extension.matchAll(pattern)) {
      const payload = match[1];
      let semantic = "";
      if (["a", "d", "c"].includes(tag)) {
        semantic = payload.includes(":") ? payload.slice(payload.indexOf(":") + 1) : payload;
      } else if (tag === "r" && !payload.includes("<")) {
        semantic = payload.includes(":") ? payload.slice(payload.indexOf(":") + 1) : payload;
      } else if (tag === "v") {
        const virtual = payload.match(/^\d+:([^:]+):\[(\d+):(\d+)\]$/);
        semantic = virtual
          ? `${virtual[1]}:[${[Number(virtual[2]), Number(virtual[3])].sort((a, b) => a - b).join(":")}]`
          : payload;
      } else if (tag === "g") {
        semantic = payload.replace(/\s+/g, "");
      } else if (tag === "s" || (tag === "r" && payload.includes("<"))) {
        // Nested E-SMILES has its own coordinate space. Until a recursive atom
        // mapper is available, preserve it exactly instead of guessing a remap.
        semantic = payload.trim();
      }
      if (semantic) tokens.push(`${tag}:${semantic}`);
    }
  }
  return tokens.sort();
}

function unknownExtensionBlocks(extension) {
  const blocks = [];
  const pattern = /<([A-Za-z][A-Za-z0-9_-]*)\b[^>]*>([\s\S]*?)<\/\1\s*>/g;
  for (const match of extension.matchAll(pattern)) {
    const tag = match[1].toLowerCase();
    if (!SUPPORTED_EXTENSION_TAGS.has(tag) && tag !== "sep") {
      blocks.push(match[0].trim());
    }
  }
  return blocks.sort();
}

function unknownExtensionTags(extension) {
  const tags = new Set();
  for (const match of extension.matchAll(/<([A-Za-z][A-Za-z0-9_-]*)\b[^>]*>/gi)) {
    const tag = match[1].toLowerCase();
    if (!SUPPORTED_EXTENSION_TAGS.has(tag)
        && !INLINE_EXTENSION_TAGS.has(tag)
        && tag !== "sep") {
      tags.add(tag);
    }
  }
  return [...tags].sort();
}

function assertBalancedExtension(extension) {
  const opened = extensionTagCounts(extension);
  const closed = new Map();
  for (const match of extension.matchAll(/<\/([A-Za-z][A-Za-z0-9_-]*)\s*>/g)) {
    const tag = match[1].toLowerCase();
    closed.set(tag, (closed.get(tag) || 0) + 1);
  }
  const tags = new Set([...opened.keys(), ...closed.keys()]);
  for (const tag of tags) {
    if (!SUPPORTED_EXTENSION_TAGS.has(tag)) continue;
    const count = opened.get(tag) || 0;
    if ((closed.get(tag) || 0) !== count) {
      throw new Error(`E-SMILES extension tag <${tag}> is not balanced`);
    }
  }
}

export function assertEsmilesRoundtrip(original, candidate) {
  const input = splitEsmiles(original);
  const outputText = String(candidate || "").trim();
  if (!outputText) throw new Error("Ketcher returned an empty E-SMILES");
  const output = splitEsmiles(outputText);
  if (!output.hasSeparator) {
    throw new Error("Ketcher output is missing the E-SMILES <sep> separator");
  }
  if (!output.body) throw new Error("Ketcher returned an empty molecule body");
  if (input.extension && !output.extension) {
    throw new Error("Ketcher dropped the non-empty E-SMILES extension");
  }
  if (input.extension) {
    assertBalancedExtension(input.extension);
    assertBalancedExtension(output.extension);
    const expected = extensionTagCounts(input.extension);
    const actual = extensionTagCounts(output.extension);
    for (const [tag, count] of expected) {
      if ((actual.get(tag) || 0) !== count) {
        throw new Error(`Ketcher changed E-SMILES <${tag}> extension count`);
      }
    }
    for (const [tag, count] of actual) {
      if ((expected.get(tag) || 0) !== count) {
        throw new Error(`Ketcher introduced unexpected E-SMILES <${tag}> metadata`);
      }
    }
    const expectedSemantics = extensionSemanticTokens(input.extension);
    const actualSemantics = extensionSemanticTokens(output.extension);
    if (JSON.stringify(expectedSemantics) !== JSON.stringify(actualSemantics)) {
      throw new Error("Ketcher changed E-SMILES labels or repeat semantics");
    }
    const expectedUnknown = unknownExtensionBlocks(input.extension);
    const actualUnknown = unknownExtensionBlocks(output.extension);
    if (JSON.stringify(expectedUnknown) !== JSON.stringify(actualUnknown)) {
      throw new Error("Ketcher changed unsupported E-SMILES metadata");
    }
    const unknownTags = unknownExtensionTags(input.extension);
    if (unknownTags.length && input.extension !== output.extension) {
      throw new Error(
        `Ketcher must preserve unsupported E-SMILES metadata exactly: ${unknownTags.join(", ")}`,
      );
    }
  }
  return outputText;
}

export async function waitForEsmilesKetcher(win, timeoutMs = 6000) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    if (win?.ketcher
        && typeof win.ketcher.setMolecule === "function"
        && typeof win.ketcher.getEsmiles === "function") {
      return win.ketcher;
    }
    await new Promise((resolve) => setTimeout(resolve, 80));
  }
  return null;
}

export async function loadEsmiles(ketcher, fullEsmiles) {
  if (!ketcher || typeof ketcher.setMolecule !== "function"
      || typeof ketcher.getEsmiles !== "function") {
    throw new Error("Pinned Ketcher E-SMILES API is unavailable");
  }
  const input = String(fullEsmiles || "").trim();
  if (!input) {
    await ketcher.setMolecule("");
    return;
  }
  await ketcher.setMolecule(input);
  assertEsmilesRoundtrip(input, await ketcher.getEsmiles());
}

export async function exportEsmiles(ketcher, original) {
  if (!ketcher || typeof ketcher.setMolecule !== "function"
      || typeof ketcher.getEsmiles !== "function") {
    throw new Error("Pinned Ketcher full E-SMILES API is unavailable");
  }
  const candidate = assertEsmilesRoundtrip(original, await ketcher.getEsmiles());

  // Parse the proposed output once more through the pinned editor before it
  // leaves the iframe. Invalid coordinates are dropped by the DP parser; the
  // second comparison turns that silent downgrade into a visible failure.
  await ketcher.setMolecule(candidate);
  const reparsed = assertEsmilesRoundtrip(candidate, await ketcher.getEsmiles());
  return assertEsmilesRoundtrip(original, reparsed);
}
