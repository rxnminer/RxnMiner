import { createHash, randomUUID } from "node:crypto";
import {
  chmod,
  copyFile,
  mkdir,
  open,
  readFile,
  rename,
  stat,
  unlink,
  writeFile,
} from "node:fs/promises";
import path from "node:path";
import type {
  ContextMode,
  LocalInputArtifact,
  LocalInputKind,
  LocalInputSelection,
} from "./contracts.js";
import { RuntimePolicyError } from "./errors.js";
import { assertNoGroundTruth } from "./security.js";

const MAX_BYTES: Record<LocalInputKind, number> = {
  figure: 50 * 1024 * 1024,
  paper: 200 * 1024 * 1024,
  si: 200 * 1024 * 1024,
};

const CANONICAL_NAMES: Record<LocalInputKind, string> = {
  figure: "figure",
  paper: "main.pdf",
  si: "si.pdf",
};

export interface MaterializedLocalInput extends LocalInputArtifact {
  absolutePath: string;
  workspacePath: string;
}

export class LocalInputStore {
  private readonly root: string;

  constructor(root: string) {
    this.root = path.resolve(root);
  }

  async stage(
    userId: string,
    input: AsyncIterable<Uint8Array>,
    metadata: {
      kind: LocalInputKind;
      originalName: string;
      mediaType: string;
      signal?: AbortSignal;
    },
  ): Promise<LocalInputArtifact> {
    if (metadata.kind !== "figure") throw new RuntimePolicyError("INVALID_INPUT", "Only reaction images are supported", 400);
    assertInputStageOpen(metadata.signal);
    const id = randomUUID();
    const directory = this.userDirectory(userId);
    await mkdir(directory, { recursive: true, mode: 0o700 });
    await chmod(directory, 0o700);
    const temporary = path.join(directory, `${id}.uploading`);
    const target = path.join(directory, `${id}.bin`);
    const descriptor = await open(temporary, "wx", 0o600);
    const hash = createHash("sha256");
    let byteSize = 0;
    const prefix: Buffer[] = [];
    let prefixLength = 0;
    try {
      const iterator = input[Symbol.asyncIterator]();
      while (true) {
        const next = await nextInputChunk(iterator, metadata.signal);
        if (next.done) break;
        const value = next.value;
        assertInputStageOpen(metadata.signal);
        const chunk = Buffer.from(value);
        byteSize += chunk.length;
        if (byteSize > MAX_BYTES[metadata.kind]) {
          throw new RuntimePolicyError(
            "INPUT_TOO_LARGE",
            `${metadata.kind} input exceeds ${MAX_BYTES[metadata.kind] / 1024 / 1024} MB`,
            413,
          );
        }
        if (prefixLength < 16) {
          const part = chunk.subarray(0, Math.min(chunk.length, 16 - prefixLength));
          prefix.push(part);
          prefixLength += part.length;
        }
        hash.update(chunk);
        await descriptor.write(chunk);
      }
      assertInputStageOpen(metadata.signal);
      await descriptor.sync();
    } catch (error) {
      await descriptor.close();
      await unlink(temporary).catch(() => {});
      throw error;
    }
    await descriptor.close();
    if (byteSize === 0) {
      await unlink(temporary).catch(() => {});
      throw new RuntimePolicyError("INVALID_INPUT", "Input file is empty", 400);
    }
    const sniffed = sniffMediaType(Buffer.concat(prefix), metadata.kind);
    if (!sniffed) {
      await unlink(temporary).catch(() => {});
      throw new RuntimePolicyError(
        "INVALID_INPUT",
        metadata.kind === "figure"
          ? "Figure input must be PNG, JPEG, WebP, or TIFF"
          : `${metadata.kind} input must be a PDF`,
        400,
      );
    }
    const metadataTarget = path.join(directory, `${id}.json`);
    try {
      assertInputStageOpen(metadata.signal);
      await rename(temporary, target);
      await chmod(target, 0o400);
      assertInputStageOpen(metadata.signal);
      const artifact: LocalInputArtifact = {
        id,
        userId,
        kind: metadata.kind,
        originalName: safeOriginalName(metadata.originalName),
        mediaType: sniffed,
        sha256: hash.digest("hex"),
        byteSize,
        createdAt: new Date().toISOString(),
      };
      assertNoGroundTruth(artifact);
      await writePrivateJson(metadataTarget, artifact);
      assertInputStageOpen(metadata.signal);
      return structuredClone(artifact);
    } catch (error) {
      // A shutdown may arrive between the atomic byte promotion and metadata
      // publication. Remove the unaddressable local upload so a rejected
      // request cannot leave durable state behind.
      await Promise.allSettled([unlink(temporary), unlink(target), unlink(metadataTarget)]);
      throw error;
    }
  }

  async get(userId: string, artifactId: string): Promise<LocalInputArtifact> {
    const id = safeArtifactId(artifactId);
    const metadataPath = path.join(this.userDirectory(userId), `${id}.json`);
    let parsed: unknown;
    try {
      parsed = JSON.parse(await readFile(metadataPath, "utf8")) as unknown;
    } catch {
      throw new RuntimePolicyError("INPUT_NOT_FOUND", "Input artifact not found", 404);
    }
    if (!isLocalInputArtifact(parsed) || parsed.userId !== userId || parsed.id !== id) {
      throw new RuntimePolicyError("INPUT_NOT_FOUND", "Input artifact not found", 404);
    }
    return structuredClone(parsed);
  }

  async read(
    userId: string,
    artifactId: string,
  ): Promise<{ artifact: LocalInputArtifact; bytes: Buffer }> {
    const artifact = await this.get(userId, artifactId);
    const source = path.join(this.userDirectory(userId), `${artifact.id}.bin`);
    await verifyArtifactFile(source, artifact);
    return { artifact, bytes: await readFile(source) };
  }

  async materialize(
    userId: string,
    contextMode: ContextMode,
    selections: LocalInputSelection[],
    workspace: string,
  ): Promise<MaterializedLocalInput[]> {
    validateSelections(contextMode, selections);
    const inputDirectory = path.join(workspace, "input");
    await mkdir(inputDirectory, { recursive: true, mode: 0o700 });
    await chmod(inputDirectory, 0o700);
    const materialized: MaterializedLocalInput[] = [];
    for (const selection of selections) {
      const artifact = await this.get(userId, selection.artifactId);
      if (artifact.kind !== selection.kind) {
        throw new RuntimePolicyError(
          "INVALID_CONTEXT",
          `Input ${artifact.id} is ${artifact.kind}, not ${selection.kind}`,
          400,
        );
      }
      const source = path.join(this.userDirectory(userId), `${artifact.id}.bin`);
      await verifyArtifactFile(source, artifact);
      const destinationName = artifact.kind === "figure"
        ? `${CANONICAL_NAMES.figure}${extensionForMediaType(artifact.mediaType)}`
        : CANONICAL_NAMES[artifact.kind];
      const destination = path.join(inputDirectory, destinationName);
      await copyFile(source, destination);
      await chmod(destination, 0o400);
      materialized.push({
        ...artifact,
        absolutePath: destination,
        workspacePath: path.posix.join("input", destinationName),
      });
    }
    return materialized;
  }

  private userDirectory(userId: string): string {
    const digest = createHash("sha256").update(userId).digest("hex");
    return path.join(this.root, digest);
  }
}

function assertInputStageOpen(signal?: AbortSignal): void {
  if (!signal?.aborted) return;
  throw new RuntimePolicyError(
    "RUNTIME_SHUTTING_DOWN",
    "The local runtime stopped before the input could be committed. Upload it again after restart.",
    503,
  );
}

function nextInputChunk(
  iterator: AsyncIterator<Uint8Array>,
  signal?: AbortSignal,
): Promise<IteratorResult<Uint8Array>> {
  if (!signal) return iterator.next();
  assertInputStageOpen(signal);
  return new Promise((resolve, reject) => {
    let settled = false;
    const onAbort = (): void => {
      if (settled) return;
      settled = true;
      reject(new RuntimePolicyError(
        "RUNTIME_SHUTTING_DOWN",
        "The local runtime stopped before the input could be committed. Upload it again after restart.",
        503,
      ));
    };
    signal.addEventListener("abort", onAbort, { once: true });
    void iterator.next().then((value) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", onAbort);
      resolve(value);
    }, (error) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", onAbort);
      reject(error);
    });
  });
}

function validateSelections(contextMode: ContextMode, selections: LocalInputSelection[]): void {
  const kinds = selections.map((selection) => selection.kind);
  if (contextMode !== "figure") throw new RuntimePolicyError("INVALID_CONTEXT", "Only figure extraction is supported", 400);
  const expected = ["figure"];
  if (
    kinds.length !== expected.length
    || expected.some((kind) => kinds.filter((value) => value === kind).length !== 1)
  ) {
    throw new RuntimePolicyError(
      "INVALID_CONTEXT",
      `${contextMode} requires exactly ${expected.join(" + ")}`,
      400,
    );
  }
}

async function verifyArtifactFile(target: string, artifact: LocalInputArtifact): Promise<void> {
  let info;
  try {
    info = await stat(target);
  } catch {
    throw new RuntimePolicyError("INPUT_NOT_FOUND", "Input artifact bytes are missing", 404);
  }
  if (!info.isFile() || info.size !== artifact.byteSize) {
    throw new RuntimePolicyError("INPUT_CORRUPT", "Input artifact size changed", 409);
  }
  const bytes = await readFile(target);
  const digest = createHash("sha256").update(bytes).digest("hex");
  if (digest !== artifact.sha256) {
    throw new RuntimePolicyError("INPUT_CORRUPT", "Input artifact hash changed", 409);
  }
}

function sniffMediaType(prefix: Buffer, kind: LocalInputKind): string | undefined {
  if (kind !== "figure") return undefined;
  if (prefix.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
    return "image/png";
  }
  if (prefix[0] === 0xff && prefix[1] === 0xd8 && prefix[2] === 0xff) return "image/jpeg";
  if (
    prefix.subarray(0, 4).toString("ascii") === "RIFF"
    && prefix.subarray(8, 12).toString("ascii") === "WEBP"
  ) return "image/webp";
  const tiff = prefix.subarray(0, 4).toString("hex");
  if (tiff === "49492a00" || tiff === "4d4d002a") return "image/tiff";
  return undefined;
}

function extensionForMediaType(mediaType: string): string {
  return ({
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/tiff": ".tiff",
  } as Record<string, string>)[mediaType] ?? ".bin";
}

function safeOriginalName(value: string): string {
  const normalized = path.basename(value || "upload").replace(/[\u0000-\u001f\u007f]/g, "");
  return normalized.slice(0, 255) || "upload";
}

function safeArtifactId(value: string): string {
  if (!/^[A-Za-z0-9-]{1,128}$/.test(value)) {
    throw new RuntimePolicyError("INPUT_NOT_FOUND", "Input artifact not found", 404);
  }
  return value;
}

function isLocalInputArtifact(value: unknown): value is LocalInputArtifact {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.id === "string"
    && typeof record.userId === "string"
    && ["figure", "paper", "si"].includes(String(record.kind))
    && typeof record.originalName === "string"
    && typeof record.mediaType === "string"
    && typeof record.sha256 === "string"
    && /^[a-f0-9]{64}$/.test(record.sha256)
    && Number.isSafeInteger(record.byteSize)
    && Number(record.byteSize) > 0
    && typeof record.createdAt === "string"
  );
}

async function writePrivateJson(target: string, value: unknown): Promise<void> {
  const temporary = `${target}.${randomUUID()}.tmp`;
  await writeFile(temporary, `${JSON.stringify(value)}\n`, { mode: 0o600, flag: "wx" });
  await rename(temporary, target);
  await chmod(target, 0o600);
}
