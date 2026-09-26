import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { createInterface } from "node:readline";
import {
  resizeImage,
  type ToolDefinition,
} from "@earendil-works/pi-coding-agent";
import type { TSchema } from "typebox";
import { chemistrySubprocessEnv } from "./subprocess-env.js";
import { assertPinnedChemistryToolContract } from "./protocol-fingerprint.js";

interface JsonRpcResponse {
  id?: number;
  result?: unknown;
  error?: { code: number; message: string };
}

interface McpToolDescriptor {
  name: string;
  description: string;
  inputSchema: TSchema;
}

interface McpToolResult {
  content: Array<{ type: string; text?: string; [key: string]: unknown }>;
  isError?: boolean;
}

interface PendingRequest {
  resolve(value: unknown): void;
  reject(error: Error): void;
  timer: NodeJS.Timeout;
}

export class ChemMcpClient {
  private nextId = 1;
  private readonly pending = new Map<number, PendingRequest>();
  private closed = false;

  private constructor(
    private readonly child: ChildProcessWithoutNullStreams,
    private readonly requestTimeoutMs: number,
    private readonly onFatal: (error: Error) => void,
  ) {
    const lines = createInterface({ input: child.stdout });
    lines.on("line", (line) => this.receive(line));
    child.once("error", (error) => {
      const unexpected = !this.closed;
      this.closed = true;
      this.failAll(error);
      if (unexpected) this.onFatal(error);
    });
    child.once("exit", (code, signal) => {
      const unexpected = !this.closed;
      this.closed = true;
      const error = new Error(`chem-extract MCP exited (${code ?? signal ?? "unknown"})`);
      this.failAll(error);
      if (unexpected) this.onFatal(error);
    });
  }

  static async start(input: {
    python: string;
    script: string;
    cwd: string;
    env?: NodeJS.ProcessEnv;
    requestTimeoutMs?: number;
    onFatal?: (error: Error) => void;
    signal?: AbortSignal;
  }): Promise<ChemMcpClient> {
    input.signal?.throwIfAborted();
    const child = spawn(input.python, [input.script], {
      cwd: input.cwd,
      env: chemistrySubprocessEnv(input.env),
      stdio: ["pipe", "pipe", "pipe"],
      detached: process.platform !== "win32",
    });
    child.stderr.on("data", () => {
      // MCP stdout is the protocol. Stderr is intentionally not persisted
      // because chemistry backends may include local paths or provider details.
    });
    const client = new ChemMcpClient(
      child,
      input.requestTimeoutMs ?? 120_000,
      input.onFatal ?? (() => {}),
    );
    try {
      await client.request("initialize", {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: "chemrxn-pi-runtime", version: "0.1.0" },
      }, input.signal);
      client.notify("notifications/initialized", {});
      return client;
    } catch (error) {
      await client.terminate();
      throw error;
    }
  }

  async listTools(): Promise<McpToolDescriptor[]> {
    const value = await this.request("tools/list", {}) as { tools?: McpToolDescriptor[] };
    return Array.isArray(value.tools) ? value.tools : [];
  }

  async callTool(
    name: string,
    args: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<McpToolResult> {
    return await this.request("tools/call", { name, arguments: args }, signal) as McpToolResult;
  }

  dispose(): void {
    void this.terminate();
  }

  async terminate(): Promise<void> {
    if (this.closed && (this.child.exitCode !== null || this.child.signalCode !== null)) return;
    this.closed = true;
    this.failAll(new Error("chem-extract MCP disposed"));
    this.child.stdin.destroy();
    if (this.child.exitCode !== null || this.child.signalCode !== null) return;
    const exited = new Promise<void>((resolve) => {
      this.child.once("exit", () => resolve());
      this.child.once("error", () => resolve());
    });
    this.killProcess("SIGTERM");
    if (await settledWithin(exited, 1_000)) return;
    this.killProcess("SIGKILL");
    await settledWithin(exited, 1_000);
  }

  private request(method: string, params: unknown, signal?: AbortSignal): Promise<unknown> {
    if (this.closed) return Promise.reject(new Error("chem-extract MCP is closed"));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const pending = this.pending.get(id);
        if (!pending) return;
        this.pending.delete(id);
        const error = new Error(`chem-extract MCP request timed out: ${method}`);
        pending.reject(error);
        this.onFatal(error);
        void this.terminate();
      }, this.requestTimeoutMs);
      timer.unref();
      const abort = () => {
        clearTimeout(timer);
        this.pending.delete(id);
        this.notify("notifications/cancelled", { requestId: id, reason: "aborted" });
        reject(new DOMException("MCP tool call aborted", "AbortError"));
      };
      if (signal?.aborted) return abort();
      signal?.addEventListener("abort", abort, { once: true });
      this.pending.set(id, {
        resolve: (value) => {
          signal?.removeEventListener("abort", abort);
          resolve(value);
        },
        reject: (error) => {
          signal?.removeEventListener("abort", abort);
          reject(error);
        },
        timer,
      });
      this.child.stdin.write(
        `${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`,
        (error) => {
          if (!error) return;
          const pending = this.pending.get(id);
          if (!pending) return;
          this.pending.delete(id);
          clearTimeout(pending.timer);
          pending.reject(error);
          this.onFatal(error);
          void this.terminate();
        },
      );
    });
  }

  private notify(method: string, params: unknown): void {
    if (this.closed || this.child.stdin.destroyed) return;
    this.child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method, params })}\n`);
  }

  private receive(line: string): void {
    let response: JsonRpcResponse;
    try {
      response = JSON.parse(line) as JsonRpcResponse;
    } catch {
      return;
    }
    if (!Number.isInteger(response.id)) return;
    const pending = this.pending.get(response.id!);
    if (!pending) return;
    this.pending.delete(response.id!);
    clearTimeout(pending.timer);
    if (response.error) {
      pending.reject(new Error(`chem-extract MCP ${response.error.code}: ${response.error.message}`));
    } else {
      pending.resolve(response.result);
    }
  }

  private failAll(error: Error): void {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
  }

  private killProcess(signal: NodeJS.Signals): void {
    const pid = this.child.pid;
    try {
      if (process.platform !== "win32" && pid) {
        process.kill(-pid, signal);
      } else {
        this.child.kill(signal);
      }
    } catch {
      // The child already exited.
    }
  }
}

async function settledWithin(promise: Promise<void>, timeoutMs: number): Promise<boolean> {
  return await Promise.race([
    promise.then(() => true),
    new Promise<boolean>((resolve) => {
      const timer = setTimeout(() => resolve(false), timeoutMs);
      timer.unref();
    }),
  ]);
}

export async function chemistryToolDefinitions(
  client: ChemMcpClient,
  allowlist: readonly string[],
  workspace?: string,
): Promise<ToolDefinition[]> {
  const allowed = new Set(allowlist);
  const discovered = await client.listTools();
  const definitions = discovered
    .filter((tool) => allowed.has(tool.name))
    .map((tool): ToolDefinition => ({
      name: tool.name,
      label: tool.name.replaceAll("_", " "),
      description: tool.description,
      promptSnippet: tool.description,
      parameters: tool.inputSchema,
      executionMode: "sequential",
      execute: async (_toolCallId, params, signal) => {
        const normalizedParams = workspace
          ? normalizeChemistryToolArguments(tool.name, params as Record<string, unknown>, workspace)
          : params as Record<string, unknown>;
        const result = await client.callTool(
          tool.name,
          normalizedParams,
          signal,
        );
        const compacted = compactChemistryContent(tool.name, result.content);
        const content = workspace
          ? await attachWorkspaceImages(tool.name, compacted, workspace)
          : compacted;
        return {
          content: content as Array<
            { type: "text"; text: string } | { type: "image"; data: string; mimeType: string }
          >,
          details: { server: "chem-extract", tool: tool.name },
          isError: result.isError === true,
        };
      },
    }));
  const missing = [...allowed].filter((name) => !definitions.some((tool) => tool.name === name));
  if (missing.length) throw new Error(`chem-extract MCP is missing allowed tools: ${missing.join(", ")}`);
  // This is the actual MCP-discovered contract that will be passed to Pi. A
  // name-only allowlist cannot detect a changed description or input schema.
  assertPinnedChemistryToolContract(definitions);
  return definitions;
}

export function normalizeChemistryToolArguments(
  toolName: string,
  raw: Record<string, unknown>,
  workspace: string,
): Record<string, unknown> {
  const args = { ...raw };
  const fields: Readonly<Record<string, readonly string[]>> = {
    detect: ["image_path", "out_dir"],
    ocsr_detect: ["image_path"],
    assemble: ["plan_path", "out_path"],
    render_reactions: ["v2_path", "image_path", "out_html"],
  };
  for (const field of fields[toolName] ?? []) {
    const value = args[field];
    if (typeof value !== "string" || !value) continue;
    const candidate = path.resolve(workspace, value.startsWith("@") ? value.slice(1) : value);
    if (!safeWorkspaceFile(workspace, candidate)) {
      throw new Error(`chemistry tool path escapes the session workspace: ${field}`);
    }
    args[field] = candidate;
  }
  return args;
}

function compactChemistryContent(
  toolName: string,
  content: McpToolResult["content"],
): McpToolResult["content"] {
  if (toolName !== "assemble") return content;
  return content.map((part) => {
    if (part.type !== "text" || typeof part.text !== "string") return part;
    try {
      const payload = JSON.parse(part.text) as Record<string, unknown>;
      if (!("v2" in payload)) return part;
      delete payload.v2;
      payload.note = "The full assembled V2 remains in v2_path; the bounded inventory is for revision and lineage audit.";
      return { ...part, text: JSON.stringify(payload) };
    } catch {
      return part;
    }
  });
}

async function attachWorkspaceImages(
  toolName: string,
  content: McpToolResult["content"],
  workspace: string,
): Promise<McpToolResult["content"]> {
  if (toolName !== "detect") return content;
  const textPart = content.find((part) => part.type === "text" && typeof part.text === "string");
  if (!textPart?.text) return content;
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(textPart.text) as Record<string, unknown>;
  } catch {
    return content;
  }
  const enriched = [...content];
  for (const field of ["overview_png", "compare_grid_png"] as const) {
    if (typeof payload[field] !== "string") continue;
    const candidate = safeWorkspaceFile(workspace, payload[field]);
    if (!candidate) continue;
    try {
      const resized = await resizeImage(await readFile(candidate), "image/png", {
        maxWidth: 2200,
        maxHeight: 2200,
        maxBytes: 4 * 1024 * 1024,
      });
      if (!resized) continue;
      enriched.push({
        type: "text",
        text: `${field}${resized.wasResized
          ? ` (resized ${resized.originalWidth}×${resized.originalHeight} → ${resized.width}×${resized.height})`
          : ""}`,
      });
      enriched.push({ type: "image", data: resized.data, mimeType: resized.mimeType });
    } catch {
      // The text result and artifact path remain available even if visual enrichment fails.
    }
  }
  return enriched;
}

function safeWorkspaceFile(workspace: string, value: string): string | undefined {
  const root = path.resolve(workspace);
  const candidate = path.resolve(value);
  const relative = path.relative(root, candidate);
  if (relative === "" || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    return undefined;
  }
  return candidate;
}
