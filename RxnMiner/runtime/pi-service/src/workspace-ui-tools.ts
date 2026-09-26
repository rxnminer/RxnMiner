import { randomUUID } from "node:crypto";
import { Type } from "typebox";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";

export const WORKSPACE_UI_TOOL_NAMES = [
  "open_chemistry_workspace",
  "propose_chemistry_correction",
] as const;

export const WORKSPACE_UI_MODULES = [
  "overview", "reactions", "attention", "ask", "saved-views",
  "evidence", "edit", "verify", "checks", "route", "table", "molecules",
] as const;

export const FIGURE_WORKSPACE_UI_MODULES = [
  "overview", "reactions", "evidence", "edit",
] as const;

const FIGURE_BBOX_KEY_PATTERN =
  "^(?:idx:[0-9]{1,9}|box:-?[0-9]+(?:\\.[0-9]+)?(?:,-?[0-9]+(?:\\.[0-9]+)?){3})$";

function validFigureBboxKey(value: string): boolean {
  if (/^idx:\d{1,9}$/.test(value)) return true;
  if (!value.startsWith("box:")) return false;
  const coordinates = value.slice(4).split(",").map(Number);
  return coordinates.length === 4
    && coordinates.every((coordinate) =>
      Number.isFinite(coordinate) && Math.abs(coordinate) <= 10_000_000)
    && coordinates[2]! > coordinates[0]!
    && coordinates[3]! > coordinates[1]!;
}

export interface WorkspaceUiCommand {
  type: "workspace.open";
  module: typeof WORKSPACE_UI_MODULES[number];
  source: "agent";
  jobId: string;
  reactionId?: string;
  anchor?: number;
  page?: number;
  documentSource?: "main" | "si";
  bboxFrame?: "pdf_points" | "page_render_px";
  bbox?: [number, number, number, number];
  bboxKey?: string;
  focus?: "selected-only" | "current-anchor" | "all-occurrences";
}

const PDF_REVIEW_VERDICTS = [
  "accept as extracted",
  "exclude from review set",
  "mark uncertain",
  "correct fields",
  "split reaction record",
  "merge with adjacent record",
] as const;

const figureChangeSchema = Type.Union([
  Type.Object({
    kind: Type.Literal("molecule"),
    side: Type.Union([Type.Literal("reactants"), Type.Literal("products")]),
    index: Type.Integer({ minimum: 0, maximum: 511 }),
    field: Type.Union([
      Type.Literal("E-SMILES"), Type.Literal("identifier"), Type.Literal("description"),
    ]),
    value: Type.String({ maxLength: 8192 }),
  }, { additionalProperties: false }),
  Type.Object({
    kind: Type.Literal("condition"),
    stepIndex: Type.Integer({ minimum: 0, maximum: 127 }),
    itemIndex: Type.Integer({ minimum: 0, maximum: 127 }),
    field: Type.Union([
      Type.Literal("role"), Type.Literal("text"),
      Type.Literal("description"), Type.Literal("E-SMILES"),
    ]),
    value: Type.String({ maxLength: 8192 }),
  }, { additionalProperties: false }),
  Type.Object({
    kind: Type.Literal("metric"),
    index: Type.Integer({ minimum: 0, maximum: 127 }),
    field: Type.Union([Type.Literal("role"), Type.Literal("text"), Type.Literal("of")]),
    value: Type.String({ maxLength: 8192 }),
  }, { additionalProperties: false }),
]);

export interface FigureReviewProposalCommand {
  type: "review.propose";
  target: "figure";
  source: "agent";
  jobId: string;
  proposalId: string;
  reactionId: string;
  rationale: string;
  evidence: string[];
  changes: Array<
    | { kind: "molecule"; side: "reactants" | "products"; index: number;
      field: "E-SMILES" | "identifier" | "description"; value: string }
    | { kind: "condition"; stepIndex: number; itemIndex: number;
      field: "role" | "text" | "description" | "E-SMILES"; value: string }
    | { kind: "metric"; index: number; field: "role" | "text" | "of"; value: string }
  >;
}

export interface PdfReviewProposalCommand {
  type: "review.propose";
  target: "pdf";
  source: "agent";
  jobId: string;
  proposalId: string;
  reactionId: string;
  rationale: string;
  evidence: string[];
  verdict: typeof PDF_REVIEW_VERDICTS[number];
  note?: string;
  correctedSmiles?: string;
  correctedCondition?: string;
}

export type ReviewProposalCommand =
  | FigureReviewProposalCommand
  | PdfReviewProposalCommand;
export type ChemistryWorkspaceCommand = WorkspaceUiCommand | ReviewProposalCommand;

export function workspaceUiToolDefinitions(input: {
  jobId: string;
  reviewTarget: "figure" | "pdf";
  modules?: readonly WorkspaceUiCommand["module"][];
  onCommand: (command: ChemistryWorkspaceCommand) => void;
}): ToolDefinition[] {
  const modules = input.modules?.length ? [...input.modules] : [...WORKSPACE_UI_MODULES];
  const openTool: ToolDefinition = {
    name: "open_chemistry_workspace",
    label: "open chemistry workspace",
    description: [
      "Open an allowlisted visual chemistry workspace module for the user.",
      "Use this after citing a reaction, anchor, page, or evidence item.",
      "This tool changes only the UI view; it never edits prediction artifacts.",
    ].join(" "),
    promptSnippet: "Open a typed chemistry view and focus its source evidence.",
    parameters: Type.Object({
      module: Type.Union(modules.map((value) => Type.Literal(value))),
      reactionId: Type.Optional(Type.String({ minLength: 1, maxLength: 320 })),
      anchor: Type.Optional(Type.Integer({ minimum: 0 })),
      ...(input.reviewTarget === "figure" ? {
        bboxKey: Type.Optional(Type.String({
            pattern: FIGURE_BBOX_KEY_PATTERN,
            maxLength: 160,
          })),
      } : {}),
      ...(input.reviewTarget === "pdf" ? {
        location: Type.Optional(Type.Union([
            Type.Object({
              documentSource: Type.Union([Type.Literal("main"), Type.Literal("si")]),
              page: Type.Integer({ minimum: 1 }),
            }, { additionalProperties: false }),
            Type.Object({
              documentSource: Type.Union([Type.Literal("main"), Type.Literal("si")]),
              page: Type.Integer({ minimum: 1 }),
              bbox: Type.Tuple([
                Type.Number({ minimum: 0, maximum: 10_000_000 }),
                Type.Number({ minimum: 0, maximum: 10_000_000 }),
                Type.Number({ minimum: 0, maximum: 10_000_000 }),
                Type.Number({ minimum: 0, maximum: 10_000_000 }),
              ]),
            }, { additionalProperties: false }),
          ])),
      } : {}),
      focus: Type.Optional(Type.Union([
        Type.Literal("selected-only"),
        Type.Literal("current-anchor"),
        Type.Literal("all-occurrences"),
      ])),
    }, { additionalProperties: false }),
    executionMode: "sequential",
    execute: async (_id, parameters) => {
      const args = parameters as {
        module: WorkspaceUiCommand["module"];
        reactionId?: string;
        anchor?: number;
        bboxKey?: string;
        location?: {
          documentSource: "main" | "si";
          page: number;
          bbox?: [number, number, number, number];
        };
        focus?: WorkspaceUiCommand["focus"];
      };
      if (
        args.location?.bbox
        && (
          args.location.bbox[2] <= args.location.bbox[0]
          || args.location.bbox[3] <= args.location.bbox[1]
        )
      ) {
        throw new Error("Evidence bbox must have positive area");
      }
      if (args.location && Number.isInteger(
        (parameters as Record<string, unknown>).page,
      )) {
        throw new Error("Use location.page instead of a second top-level page");
      }
      if (args.location && Number.isInteger(args.anchor)) {
        throw new Error("Anchor and document location are mutually exclusive");
      }
      if (args.bboxKey && !validFigureBboxKey(args.bboxKey)) {
        throw new Error("Figure bbox key must identify a valid positive-area box");
      }
      const command: WorkspaceUiCommand = {
        type: "workspace.open",
        module: args.module,
        source: "agent",
        jobId: input.jobId,
        ...(args.reactionId ? { reactionId: args.reactionId } : {}),
        ...(Number.isInteger(args.anchor) ? { anchor: args.anchor } : {}),
        ...(args.bboxKey ? { bboxKey: args.bboxKey } : {}),
        ...(args.location ? {
          documentSource: args.location.documentSource,
          page: args.location.page,
          ...(args.location.bbox ? {
            bboxFrame: args.location.documentSource === "si"
              ? "page_render_px" as const : "pdf_points" as const,
          } : {}),
          ...(args.location.bbox ? { bbox: args.location.bbox } : {}),
        } : {}),
        ...(args.focus ? { focus: args.focus } : {}),
      };
      input.onCommand(command);
      return {
        content: [{ type: "text" as const,
          text: `Opened ${command.module} in the user's chemistry workspace.` }],
        details: { workspaceCommand: command },
      };
    },
  };

  const common = {
    reactionId: Type.String({ minLength: 1, maxLength: 320 }),
    rationale: Type.String({ minLength: 1, maxLength: 4000 }),
    evidence: Type.Array(Type.String({ minLength: 1, maxLength: 1000 }), {
      maxItems: 24,
    }),
  };
  const parameters = input.reviewTarget === "figure"
    ? Type.Object({
        ...common,
        changes: Type.Array(figureChangeSchema, { minItems: 1, maxItems: 32 }),
      }, { additionalProperties: false })
    : Type.Object({
        ...common,
        verdict: Type.Union(PDF_REVIEW_VERDICTS.map((value) => Type.Literal(value))),
        note: Type.Optional(Type.String({ maxLength: 8000 })),
        correctedSmiles: Type.Optional(Type.String({ maxLength: 8192 })),
        correctedCondition: Type.Optional(Type.String({ maxLength: 8192 })),
      }, { additionalProperties: false });
  const proposalTool: ToolDefinition = {
    name: "propose_chemistry_correction",
    label: "propose chemistry correction",
    description: [
      "Stage an allowlisted chemistry correction in the user's visual review workspace.",
      "The proposal is visibly marked not applied and cannot write prediction artifacts.",
      "Only the user can Apply it after reviewing the diff.",
    ].join(" "),
    promptSnippet: "Propose a typed correction with rationale and evidence; never claim it was applied.",
    parameters,
    executionMode: "sequential",
    execute: async (_id, rawParameters) => {
      const args = rawParameters as Record<string, unknown>;
      const command = {
        ...args,
        type: "review.propose",
        target: input.reviewTarget,
        source: "agent",
        jobId: input.jobId,
        proposalId: randomUUID(),
      } as ChemistryWorkspaceCommand;
      input.onCommand(command);
      return {
        content: [{ type: "text" as const,
          text: "Correction proposal opened for human review. It has not been applied." }],
        details: { workspaceCommand: command },
      };
    },
  };
  return [openTool, proposalTool];
}
