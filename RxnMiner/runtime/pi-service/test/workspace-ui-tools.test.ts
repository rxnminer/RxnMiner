import assert from "node:assert/strict";
import { test } from "node:test";
import {
  FIGURE_WORKSPACE_UI_MODULES,
  WORKSPACE_UI_MODULES,
  workspaceUiToolDefinitions,
  type ChemistryWorkspaceCommand,
  type WorkspaceUiCommand,
} from "../src/workspace-ui-tools.js";

test("workspace UI tool emits only an allowlisted typed view command", async () => {
  const commands: ChemistryWorkspaceCommand[] = [];
  const [tool] = workspaceUiToolDefinitions({
    jobId: "paper-50",
    reviewTarget: "pdf",
    onCommand: (command) => commands.push(command),
  });
  assert.equal(tool?.name, "open_chemistry_workspace");
  const schema = tool?.parameters as {
    properties?: Record<string, unknown>;
  };
  assert.equal(schema.properties?.page, undefined);
  assert.ok(schema.properties?.location);
  const result = await tool!.execute(
    "workspace-1",
    {
      module: "evidence",
      reactionId: "pred:1:12",
      location: {
        documentSource: "si",
        page: 25,
        bbox: [745, 1044, 856, 1189],
      },
      focus: "selected-only",
    },
    undefined,
    () => {},
    {} as never,
  );
  assert.deepEqual(commands, [{
    type: "workspace.open",
    module: "evidence",
    source: "agent",
    jobId: "paper-50",
    reactionId: "pred:1:12",
    page: 25,
    documentSource: "si",
    bboxFrame: "page_render_px",
    bbox: [745, 1044, 856, 1189],
    focus: "selected-only",
  }]);
  assert.equal("html" in commands[0]!, false);
  assert.match((result.content[0] as { text: string }).text, /Opened evidence/);
});

test("workspace UI module allowlist matches the adaptive product contract", () => {
  assert.deepEqual(WORKSPACE_UI_MODULES, [
    "overview", "reactions", "attention", "ask", "saved-views",
    "evidence", "edit", "verify", "checks", "route", "table", "molecules",
  ]);
});

test("Figure sessions expose only modules their renderer consumes", () => {
  const [tool] = workspaceUiToolDefinitions({
    jobId: "figure-1",
    reviewTarget: "figure",
    modules: FIGURE_WORKSPACE_UI_MODULES,
    onCommand: () => {},
  });
  const schema = tool?.parameters as {
    properties?: Record<string, unknown> & {
      module?: { anyOf?: Array<{ const?: string }> };
    };
  };
  assert.deepEqual(
    schema.properties?.module?.anyOf?.map((item) => item.const),
    ["overview", "reactions", "evidence", "edit"],
  );
  assert.ok(schema.properties?.bboxKey);
  assert.equal(schema.properties?.location, undefined);
});

test("Figure workspace tool emits an exact stable bbox key", async () => {
  const commands: ChemistryWorkspaceCommand[] = [];
  const [tool] = workspaceUiToolDefinitions({
    jobId: "figure-1",
    reviewTarget: "figure",
    modules: FIGURE_WORKSPACE_UI_MODULES,
    onCommand: (command) => commands.push(command),
  });
  await tool!.execute(
    "workspace-figure-bbox",
    {
      module: "evidence",
      reactionId: "5",
      bboxKey: "idx:0",
    },
    undefined,
    () => {},
    {} as never,
  );
  assert.deepEqual(commands, [{
    type: "workspace.open",
    module: "evidence",
    source: "agent",
    jobId: "figure-1",
    reactionId: "5",
    bboxKey: "idx:0",
  }]);
});

test("PDF workspace locations reject inverted boxes before a command is emitted", async () => {
  const commands: ChemistryWorkspaceCommand[] = [];
  const [tool] = workspaceUiToolDefinitions({
    jobId: "paper-50",
    reviewTarget: "pdf",
    onCommand: (command) => commands.push(command),
  });
  await assert.rejects(() => tool!.execute(
    "workspace-invalid-box",
    {
      module: "evidence",
      location: {
        documentSource: "si",
        page: 25,
        bbox: [856, 1044, 745, 1189],
      },
    },
    undefined,
    () => {},
    {} as never,
  ), /positive area/);
  assert.deepEqual(commands, []);
  await assert.rejects(() => tool!.execute(
    "workspace-conflicting-page",
    {
      module: "evidence",
      page: 2,
      location: {
        documentSource: "main",
        page: 3,
      },
    },
    undefined,
    () => {},
    {} as never,
  ), /location\.page/);
  assert.deepEqual(commands, []);
  await assert.rejects(() => tool!.execute(
    "workspace-conflicting-anchor-location",
    {
      module: "evidence",
      anchor: 1,
      location: {
        documentSource: "main",
        page: 3,
      },
    },
    undefined,
    () => {},
    {} as never,
  ), /mutually exclusive/);
  assert.deepEqual(commands, []);
});

test("Figure workspace tool rejects invalid coordinate keys before emission", async () => {
  const commands: ChemistryWorkspaceCommand[] = [];
  const [tool] = workspaceUiToolDefinitions({
    jobId: "figure-1",
    reviewTarget: "figure",
    modules: FIGURE_WORKSPACE_UI_MODULES,
    onCommand: (command) => commands.push(command),
  });
  await assert.rejects(() => tool!.execute(
    "workspace-invalid-figure-box",
    {
      module: "evidence",
      bboxKey: "box:100,100,50,50",
    },
    undefined,
    () => {},
    {} as never,
  ), /positive-area box/);
  assert.deepEqual(commands, []);
});

test("correction tool emits a typed Figure proposal with no Apply or markup channel", async () => {
  const commands: Array<Record<string, unknown>> = [];
  const tools = workspaceUiToolDefinitions({
    jobId: "figure-1",
    reviewTarget: "figure",
    modules: FIGURE_WORKSPACE_UI_MODULES,
    onCommand: (command) => commands.push(command as unknown as Record<string, unknown>),
  });
  const tool = tools.find((item) => item.name === "propose_chemistry_correction");
  const result = await tool!.execute("proposal-1", {
    reactionId: "3b",
    rationale: "The source wedge is inverted relative to the extracted product.",
    evidence: ["Figure bbox 4", "raw MolParser detection 4"],
    changes: [{
      kind: "molecule",
      side: "products",
      index: 0,
      field: "E-SMILES",
      value: "C[C@@H](O)Cl",
    }],
    type: "workspace.render-html",
    target: "pdf",
    source: "system",
    jobId: "another-job",
  }, undefined, () => {}, {} as never);
  assert.equal(commands[0]?.type, "review.propose");
  assert.match(String(commands[0]?.proposalId), /^[A-Za-z0-9._:-]+$/);
  assert.equal(commands[0]?.target, "figure");
  assert.equal(commands[0]?.jobId, "figure-1");
  assert.equal("apply" in commands[0]!, false);
  assert.equal("html" in commands[0]!, false);
  assert.match((result.content[0] as { text: string }).text, /not been applied/);
});

test("PDF proposal schema requires a human-review verdict", () => {
  const [, tool] = workspaceUiToolDefinitions({
    jobId: "paper-50",
    reviewTarget: "pdf",
    onCommand: () => {},
  });
  const schema = tool!.parameters as {
    required?: string[];
  };
  assert.ok(schema.required?.includes("verdict"));
});
