import { rmSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
const root = fileURLToPath(new URL("../", import.meta.url));
const target = path.resolve(root, "dist");
if (path.dirname(target) !== path.resolve(root)) throw new Error("Invalid build output path");
rmSync(target, { recursive: true, force: true });
