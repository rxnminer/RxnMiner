// Single-instance RDKit.js loader.
// Returns a promise that resolves to the global RDKit module.
// Caller passes a SMILES, gets back an SVG string (or throws).

let _rdkitPromise = null;
let _rdkitScriptRequested = false;
let _rdkitScriptError = null;

function _ensureScript() {
  if (typeof window.initRDKitModule === "function" || _rdkitScriptRequested) return;
  _rdkitScriptRequested = true;
  const existing = document.querySelector(
    'script[src$="/static/lib/RDKit_minimal.js"],script[data-chemrxn-rdkit]'
  );
  if (existing) return;
  const script = document.createElement("script");
  script.src = "/static/lib/RDKit_minimal.js";
  script.async = true;
  script.dataset.chemrxnRdkit = "true";
  script.addEventListener("error", () => {
    _rdkitScriptError = new Error("RDKit_minimal.js failed to load");
  }, { once: true });
  document.head.appendChild(script);
}

function _waitInit() {
  if (typeof window.initRDKitModule !== "function") {
    _ensureScript();
    return new Promise((resolve, reject) => {
      const startedAt = Date.now();
      const tick = () => {
        if (typeof window.initRDKitModule === "function") return resolve();
        if (_rdkitScriptError) return reject(_rdkitScriptError);
        if (Date.now() - startedAt > 10000)
          return reject(new Error("RDKit_minimal.js never registered initRDKitModule (10s timeout)"));
        setTimeout(tick, 60);
      };
      tick();
    });
  }
  return Promise.resolve();
}

export function loadRDKit() {
  if (_rdkitPromise) return _rdkitPromise;
  _rdkitPromise = (async () => {
    await _waitInit();
    const RDKit = await window.initRDKitModule({
      // Serve WASM locally so structure previews do not depend on CDN availability.
      locateFile: () => "/static/lib/RDKit_minimal.wasm",
    });
    return RDKit;
  })();
  return _rdkitPromise;
}

// Strip our internal annotations so RDKit can parse it: drop trailing `<sep>...`
// (everything after `<sep>` is Markush metadata not standard SMILES) and
// also drop `<a>` tags inside tagged Markush expressions for a best-effort
// fallback render.
export function cleanSmilesForRender(esmiles) {
  if (!esmiles) return "";
  // Discard everything from <sep> onward
  let s = String(esmiles).split("<sep>")[0];
  // Defensive: drop any remaining tag content we don't understand
  s = s.replace(/<[^>]+>/g, "");
  return s.trim();
}

export async function canonicalSmiles(smiles) {
  const cleaned = cleanSmilesForRender(smiles);
  if (!cleaned) return null;
  const RDKit = await loadRDKit();
  let mol = null;
  try {
    mol = RDKit.get_mol(cleaned);
    if (!mol || !mol.is_valid()) return null;
    return mol.get_smiles();
  } catch (e) {
    return null;
  } finally {
    if (mol) mol.delete();
  }
}

export async function sameConnectivity(smilesA, smilesB) {
  const a = cleanSmilesForRender(smilesA);
  const b = cleanSmilesForRender(smilesB);
  if (!a || !b) return false;
  const RDKit = await loadRDKit();
  let mol = null;
  let query = null;
  try {
    mol = RDKit.get_mol(a);
    query = RDKit.get_qmol(b);
    if (!mol?.is_valid() || !query?.is_valid()) return false;
    if (mol.get_num_atoms() !== query.get_num_atoms()) return false;
    if (mol.get_num_bonds() !== query.get_num_bonds()) return false;
    const match = JSON.parse(mol.get_substruct_match(query) || "{}");
    return (match.atoms || []).length === mol.get_num_atoms()
      && (match.bonds || []).length === mol.get_num_bonds();
  } catch (e) {
    return false;
  } finally {
    if (mol) mol.delete();
    if (query) query.delete();
  }
}

export async function renderSmilesToSvg(smiles, opts = {}) {
  const { width = 220, height = 160 } = opts;
  const cleaned = cleanSmilesForRender(smiles);
  if (!cleaned) return null;
  const RDKit = await loadRDKit();
  let mol = null;
  try {
    mol = RDKit.get_mol(cleaned);
    if (!mol || !mol.is_valid()) {
      return null;
    }
    const svg = mol.get_svg(width, height);
    return svg;
  } catch (e) {
    return null;
  } finally {
    if (mol) mol.delete();
  }
}
