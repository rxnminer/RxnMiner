import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { StringDecoder } from "node:string_decoder";
import { fileURLToPath } from "node:url";
import { dirname, isAbsolute, resolve } from "node:path";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
  DEFAULT_MAX_BYTES,
  DEFAULT_MAX_LINES,
  formatSize,
  truncateHead,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const EXTENSION_DIR = dirname(fileURLToPath(import.meta.url));
const SKILLS_ROOT = resolve(EXTENSION_DIR, "..");
const RUNNER = resolve(SKILLS_ROOT, "scripts", "run_chem_mcp.py");
const REQUEST_TIMEOUT_MS = 15 * 60 * 1000;

type RpcResponse = {
  id?: number;
  result?: unknown;
  error?: { code?: number; message?: string };
};

type McpToolResult = {
  content?: Array<{ type: string; text?: string }>;
  isError?: boolean;
};

type PendingRequest = {
  resolve: (value: unknown) => void;
  reject: (reason: Error) => void;
  timer: NodeJS.Timeout;
};

class ChemMcpClient {
  private child?: ChildProcessWithoutNullStreams;
  private startPromise?: Promise<void>;
  private nextId = 1;
  private pending = new Map<number, PendingRequest>();
  private stderrTail = "";

  async start(): Promise<void> {
    // A spawned process is not ready until the initialize handshake resolves. Concurrent first
    // calls must await that same handshake instead of sending tools/call prematurely.
    if (this.startPromise) return this.startPromise;
    if (this.child && !this.child.killed) return;
    this.startPromise = this.startInner();
    try {
      await this.startPromise;
    } finally {
      this.startPromise = undefined;
    }
  }

  private async startInner(): Promise<void> {
    const python = process.env.RXN_SKILLS_PYTHON || "python3";
    const child = spawn(python, [RUNNER], {
      cwd: SKILLS_ROOT,
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child = child;
    this.stderrTail = "";

    child.stderr.on("data", (chunk: Buffer) => {
      this.stderrTail = (this.stderrTail + chunk.toString("utf8")).slice(-20000);
    });
    child.on("error", (error) => this.failAll(error));
    child.on("exit", (code, signal) => {
      const suffix = this.stderrTail.trim() ? `\n${this.stderrTail.trim()}` : "";
      this.failAll(new Error(`chem-extract MCP exited (code=${code}, signal=${signal})${suffix}`));
      if (this.child === child) this.child = undefined;
    });

    // MCP uses strict LF-delimited JSON. Do not use node:readline: it also treats Unicode
    // line separators as records, and those characters are valid inside chemistry text fields.
    const decoder = new StringDecoder("utf8");
    let stdoutBuffer = "";
    child.stdout.on("data", (chunk: Buffer) => {
      stdoutBuffer += decoder.write(chunk);
      while (true) {
        const newline = stdoutBuffer.indexOf("\n");
        if (newline < 0) break;
        let line = stdoutBuffer.slice(0, newline);
        stdoutBuffer = stdoutBuffer.slice(newline + 1);
        if (line.endsWith("\r")) line = line.slice(0, -1);
        if (line) this.handleLine(line);
      }
    });
    child.stdout.on("end", () => {
      stdoutBuffer += decoder.end();
      if (stdoutBuffer) this.handleLine(stdoutBuffer.endsWith("\r") ? stdoutBuffer.slice(0, -1) : stdoutBuffer);
    });

    await this.requestRaw("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "pi-chem-extract", version: "1.0.0" },
    });
    this.notify("notifications/initialized", {});
  }

  private handleLine(line: string): void {
    let response: RpcResponse;
    try {
      response = JSON.parse(line) as RpcResponse;
    } catch {
      return;
    }
    if (typeof response.id !== "number") return;
    const pending = this.pending.get(response.id);
    if (!pending) return;
    this.pending.delete(response.id);
    clearTimeout(pending.timer);
    if (response.error) {
      pending.reject(new Error(response.error.message || `MCP error ${response.error.code ?? ""}`));
    } else {
      pending.resolve(response.result);
    }
  }

  private requestRaw(method: string, params: unknown): Promise<unknown> {
    const child = this.child;
    if (!child || child.killed) return Promise.reject(new Error("chem-extract MCP is not running"));
    const id = this.nextId++;
    return new Promise((resolvePromise, rejectPromise) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        rejectPromise(new Error(`chem-extract MCP request timed out after ${REQUEST_TIMEOUT_MS / 1000}s`));
      }, REQUEST_TIMEOUT_MS);
      this.pending.set(id, { resolve: resolvePromise, reject: rejectPromise, timer });
      child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
    });
  }

  private notify(method: string, params: unknown): void {
    const child = this.child;
    if (!child || child.killed) return;
    child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method, params })}\n`);
  }

  async callTool(name: string, args: unknown, signal?: AbortSignal): Promise<McpToolResult> {
    await this.start();
    if (signal?.aborted) throw new Error("Cancelled");

    const request = this.requestRaw("tools/call", { name, arguments: args }) as Promise<McpToolResult>;
    if (!signal) return request;

    return new Promise((resolvePromise, rejectPromise) => {
      const onAbort = () => {
        this.notify("notifications/cancelled", { reason: "Pi tool call cancelled" });
        rejectPromise(new Error("Cancelled"));
      };
      signal.addEventListener("abort", onAbort, { once: true });
      request.then(resolvePromise, rejectPromise).finally(() => {
        signal.removeEventListener("abort", onAbort);
      });
    });
  }

  stop(): void {
    const child = this.child;
    this.child = undefined;
    if (!child || child.killed) return;
    child.stdin.end();
    setTimeout(() => {
      if (!child.killed) child.kill("SIGTERM");
    }, 1000).unref();
  }

  private failAll(reason: Error): void {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(reason);
    }
    this.pending.clear();
  }
}

function cleanPath(value: string, cwd: string): string {
  const stripped = value.startsWith("@") ? value.slice(1) : value;
  if (!stripped) return stripped;
  return isAbsolute(stripped) ? stripped : resolve(cwd, stripped);
}

function normalizePaths(name: string, raw: Record<string, unknown>, cwd: string): Record<string, unknown> {
  const args = { ...raw };
  const fieldsByTool: Record<string, string[]> = {
    detect: ["image_path", "out_dir"],
    ocsr_detect: ["image_path"],
    assemble: ["plan_path", "out_path"],
    render_reactions: ["v2_path", "image_path", "out_html"],
  };
  for (const field of fieldsByTool[name] || []) {
    const value = args[field];
    if (typeof value === "string" && value) args[field] = cleanPath(value, cwd);
  }
  return args;
}

async function compactToolOutput(name: string, result: McpToolResult) {
  const rawText = (result.content || [])
    .filter((item) => item.type === "text" && typeof item.text === "string")
    .map((item) => item.text as string)
    .join("\n");
  if (result.isError) throw new Error(rawText || `${name} failed`);

  let data: Record<string, unknown> | undefined;
  try {
    data = JSON.parse(rawText) as Record<string, unknown>;
  } catch {
    data = undefined;
  }

  if (data && name === "assemble" && "v2" in data) {
    delete data.v2;
    data.note = "Full assembled V2 is stored at v2_path; use read on that file when needed.";
  }
  if (data && name === "verify_smiles"
      && (typeof data.svg === "string" || typeof data.indexed_svg === "string")) {
    const directory = await mkdtemp(join(tmpdir(), "pi-chem-verify-"));
    if (typeof data.svg === "string") {
      const svgPath = join(directory, "molecule.svg");
      await writeFile(svgPath, data.svg, "utf8");
      delete data.svg;
      data.svg_path = svgPath;
    }
    if (typeof data.indexed_svg === "string") {
      const indexedSvgPath = join(directory, "molecule.indexed.svg");
      await writeFile(indexedSvgPath, data.indexed_svg, "utf8");
      delete data.indexed_svg;
      data.indexed_svg_path = indexedSvgPath;
    }
  }

  const fullText = data ? JSON.stringify(data, null, 2) : rawText;
  const truncation = truncateHead(fullText, {
    maxLines: DEFAULT_MAX_LINES,
    maxBytes: DEFAULT_MAX_BYTES,
  });
  let text = truncation.content;
  let fullOutputPath: string | undefined;
  if (truncation.truncated) {
    const directory = await mkdtemp(join(tmpdir(), `pi-chem-${name}-`));
    fullOutputPath = join(directory, "output.json");
    await writeFile(fullOutputPath, fullText, "utf8");
    text += `\n\n[Output truncated to ${truncation.outputLines} lines / ${formatSize(truncation.outputBytes)}. `;
    text += `Full output: ${fullOutputPath}]`;
  }

  const content: Array<
    | { type: "text"; text: string }
    | { type: "image"; data: string; mimeType: string }
  > = [{ type: "text", text }];
  if (data && name === "verify_smiles") {
    // Make visual inspection a tool guarantee rather than a second model action. Returning only
    // paths led dense reviews to consume RDKit metadata without ever opening every structure.
    for (const key of ["render_png_path", "indexed_render_png_path"] as const) {
      const imagePath = data[key];
      if (typeof imagePath !== "string" || !imagePath) continue;
      try {
        content.push({
          type: "image",
          data: (await readFile(imagePath)).toString("base64"),
          mimeType: "image/png",
        });
      } catch {
        // Keep the path in the text/details output. The caller can fail closed if the attachment is
        // unavailable; a missing preview must never turn chemical validation into a false success.
      }
    }
  }

  return {
    content,
    details: {
      tool: name,
      fullOutputPath,
      paths: data
        ? Object.fromEntries(
            Object.entries(data).filter(
              ([key, value]) => (key.endsWith("_path") || key.endsWith("_png") || key === "v2_path") && typeof value === "string",
            ),
          )
        : {},
    },
  };
}

const schemas = {
  detect: Type.Object({
    image_path: Type.String({ description: "Path to one chemistry figure" }),
    out_dir: Type.Optional(Type.String({ description: "Directory for overview/grid outputs" })),
  }),
  ocsr_detect: Type.Object({
    image_path: Type.String({ description: "Path to one chemistry figure" }),
  }),
  reverse_rgroup: Type.Object({
    template: Type.String({ description: "Generic Markush template E-SMILES" }),
    concrete: Type.String({ description: "Concrete molecule SMILES" }),
  }),
  fill_rgroup: Type.Object({
    template: Type.String({ description: "Generic Markush template E-SMILES" }),
    r_map: Type.Any({ description: "R-label to fragment mapping, or a list of mappings" }),
  }),
  reverse_assemble: Type.Object({
    target_product: Type.String({ description: "Concrete product SMILES" }),
    product_template: Type.String({ description: "Generic product template E-SMILES" }),
    reactant_templates: Type.Array(Type.String(), { description: "Generic reactant template E-SMILES values" }),
  }),
  verify_smiles: Type.Object({
    smiles: Type.String({ description: "SMILES or E-SMILES to validate and render" }),
  }),
  assemble: Type.Object({
    plan: Type.Optional(Type.Any({ description: "Intermediate plan object with selected_examples and reaction_templates" })),
    plan_path: Type.Optional(Type.String({ description: "Path to an intermediate-plan JSON file; preferred for large plans" })),
    out_path: Type.Optional(Type.String({ description: "Destination V2 JSON path" })),
  }),
  render_reactions: Type.Object({
    v2_path: Type.String({ description: "Assembled V2 JSON path" }),
    image_path: Type.Optional(Type.String({ description: "Original figure path" })),
    out_html: Type.Optional(Type.String({ description: "Destination review HTML path" })),
  }),
};

const descriptions: Record<keyof typeof schemas, { label: string; description: string }> = {
  detect: {
    label: "Detect reaction structures",
    description: "First step for reaction extraction: run/reuse MolParser OCSR and produce an annotated overview plus comparison grid. Open the returned PNGs before extracting.",
  },
  ocsr_detect: {
    label: "Raw OCSR detect",
    description: "Return MolParser detections for one chemistry figure without the read-first visualization workflow.",
  },
  reverse_rgroup: {
    label: "Reverse R-group",
    description: "Extract R-group fragments by comparing a generic Markush template with a concrete molecule.",
  },
  fill_rgroup: {
    label: "Fill R-group",
    description: "Materialize one or more concrete molecules by filling a Markush template with R-group mappings.",
  },
  reverse_assemble: {
    label: "Reverse assemble reactants",
    description: "Use FHR high-level reverse assembly to derive concrete reactants from a concrete product and generic templates. Partial/dummy-bearing output fails closed.",
  },
  verify_smiles: {
    label: "Verify SMILES",
    description: "Validate a SMILES/E-SMILES with RDKit, report CIP/E-Z and semantics, and automatically attach clean plus atom-indexed PNG renders for source comparison.",
  },
  assemble: {
    label: "Assemble reaction V2",
    description: "Convert the extraction plan into strict V2 JSON with validation, provenance, identifiers, templates, metrics, and topology. Pass plan_path for large plans; do not import the Python engine through bash.",
  },
  render_reactions: {
    label: "Render reaction extraction",
    description: "Render an assembled V2 extraction to a self-contained HTML for final visual review.",
  },
};

export default function (pi: ExtensionAPI) {
  const client = new ChemMcpClient();

  for (const name of Object.keys(schemas) as Array<keyof typeof schemas>) {
    const metadata = descriptions[name];
    pi.registerTool({
      name,
      label: metadata.label,
      description: metadata.description,
      parameters: schemas[name],
      async execute(_toolCallId, params, signal, onUpdate, ctx) {
        onUpdate?.({
          content: [{ type: "text", text: `${name}: starting chem-extract runtime...` }],
          details: { tool: name },
        });
        const args = normalizePaths(name, params as Record<string, unknown>, ctx.cwd);
        const result = await client.callTool(name, args, signal);
        return compactToolOutput(name, result);
      },
    });
  }

  pi.on("session_shutdown", () => {
    client.stop();
  });
}
