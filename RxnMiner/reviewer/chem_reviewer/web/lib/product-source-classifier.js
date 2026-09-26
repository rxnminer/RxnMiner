const IMAGE_NAME_RE = /\.(?:png|jpe?g|webp|tiff?)$/i;
const IMAGE_TYPE_RE = /^image\/(?:png|jpeg|webp|tiff?)$/i;

export function classifyProductSources(files) {
  const items = Array.from(files || []).filter(Boolean);
  const empty = { sourceType: null, assignments: [], needsConfirmation: false };
  if (!items.length) return { ...empty, ok: true, message: "" };
  if (items.length !== 1) return { ...empty, ok: false, message: "Choose one reaction image." };
  const file = items[0];
  if (!IMAGE_NAME_RE.test(file.name || "") || (file.type && !IMAGE_TYPE_RE.test(file.type))) {
    return { ...empty, ok: false, message: "Choose a PNG, JPEG, WebP, or TIFF reaction image." };
  }
  return { ok: true, sourceType: "figure", assignments: [{ file, role: "figure", reason: "image" }],
    needsConfirmation: false, message: "Reaction figure ready." };
}

export function submissionFromAssignments(assignments) {
  const rows = Array.from(assignments || []);
  const primary = rows.length === 1 && rows[0].role === "figure" ? rows[0].file : null;
  return { sourceType: primary ? "figure" : null, primary, si: null };
}
