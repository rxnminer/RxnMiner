#!/usr/bin/env node

import { createHash } from "node:crypto";
import { appendFileSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

function option(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

const port = Number.parseInt(option("--port") || "14311", 10);
const logPath = option("--log");
if (!Number.isSafeInteger(port) || port < 1024 || port > 65535 || !logPath) {
  throw new Error("Usage: pi-loopback-server.mjs --port PORT --log REQUESTS.jsonl");
}

let turn = 0;
const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(scriptDirectory, "../../..");
const figureMolparserFixture = JSON.parse(readFileSync(path.join(
  repositoryRoot,
  "runtime/pi-service/test/fixtures/figure/demo-figure/molparser.json",
), "utf8"));
const figureMolparserByBbox = new Map(figureMolparserFixture.map((item) => [item.bbox_index, item]));
function sanitizedMessages(messages) {
  if (!Array.isArray(messages)) return [];
  return messages.map((message) => ({
    role: message?.role,
    content: message?.role === "tool"
      ? [{ type: "tool_result", omitted: true }]
      : Array.isArray(message?.content)
      ? message.content.map((part) => part?.type === "text"
        ? { type: "text", text: part.text }
        : { type: part?.type || "attachment", omitted: true })
      : message?.content,
  }));
}

function messageText(message) {
  if (typeof message?.content === "string") return message.content;
  return Array.isArray(message?.content)
    ? message.content
      .filter((part) => part?.type === "text" && typeof part.text === "string")
      .map((part) => part.text)
      .join("")
    : "";
}

function toolCall(base, name, args, callIndex) {
  const callId = `call_chemrxn_${base.id}_${callIndex}`;
  return [
    {
      ...base,
      choices: [{
        index: 0,
        delta: {
          role: "assistant",
          tool_calls: [{
            index: 0,
            id: callId,
            type: "function",
            function: { name, arguments: JSON.stringify(args) },
          }],
        },
        finish_reason: null,
      }],
    },
    {
      ...base,
      choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }],
      usage: { prompt_tokens: 20, completion_tokens: 12, total_tokens: 32 },
    },
  ];
}

function extractionPlan() {
  return {
    selected_examples: [{
      label: "5",
      reactants: [
        {
          smiles: "CCOC(=O)OC/C=C/c1ccccc1",
          identifier: "p0",
          bbox_index: 0,
          molparser_md5: figureMolparserByBbox.get(0)?.md5,
          amount: "1.0 equiv",
        },
        {
          smiles: "CCOC(=O)/C=C/c1ccccc1",
          identifier: "p1",
          bbox_index: 1,
          molparser_md5: figureMolparserByBbox.get(1)?.md5,
          amount: "2.0 equiv",
        },
      ],
      products: [{
        smiles: "C=C[C@H](c1ccccc1)[C@@H](C(=O)OCC)[C@H](O)c1ccccc1",
        identifier: "5",
        bbox_index: 3,
        molparser_md5: figureMolparserByBbox.get(3)?.md5,
      }],
      conditions: [[
        { role: "catalyst", text: "5 mol% [Ir-(R,R,R)]" },
        { role: "catalyst", text: "3 mol% Cu(MeCN)4PF6" },
        { role: "ligand", text: "6 mol% (S,S)-FOXAP" },
        { role: "solvent", text: "THF (0.07 M)" },
        { role: "temperature", text: "rt" },
        { role: "time", text: "16 h" },
      ]],
      metrics: { yield: "84%", dr: "98:2", er: ">99.5:0.5" },
      description: "Compound 5; deterministic loopback acceptance extraction.",
    }],
  };
}

const server = createServer(async (request, response) => {
  if (request.method === "GET" && request.url === "/healthz") {
    response.writeHead(200, { "content-type": "application/json" });
    response.end('{"status":"ok"}');
    return;
  }
  if (request.method !== "POST" || request.url !== "/v1/chat/completions") {
    response.writeHead(404, { "content-type": "application/json" });
    response.end('{"error":"not found"}');
    return;
  }

  const chunks = [];
  for await (const chunk of request) chunks.push(Buffer.from(chunk));
  let body;
  try {
    body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    response.writeHead(400, { "content-type": "application/json" });
    response.end('{"error":"invalid json"}');
    return;
  }
  turn += 1;
  appendFileSync(logPath, `${JSON.stringify({
    turn,
    model: body?.model,
    messages: sanitizedMessages(body?.messages),
    // Preserve a proof about the exact provider-bound context without
    // persisting tool-result bodies or other potentially sensitive content.
    groundTruthPresent: JSON.stringify(body?.messages || []).includes("GT_SENTINEL"),
    authorizationPresent: typeof request.headers.authorization === "string",
  })}\n`, { encoding: "utf8", mode: 0o600 });

  const messages = Array.isArray(body?.messages) ? body.messages : [];
  const latestUser = [...messages].reverse().find((message) => message?.role === "user");
  const latestText = messageText(latestUser);
  const requestedModel = String(body?.model || "");
  const isToolAcceptance = requestedModel.includes("tool-check");
  const isFigureReviewFollowup = isToolAcceptance
    && latestText.includes("Figure correction acceptance");
  const toolResults = messages.filter((message) => message?.role === "tool");
  const answer = latestText.includes("Second browser turn")
    ? "Loopback follow-up response restored the first durable turn."
    : "Loopback extraction response is ready for a persisted follow-up.";
  const base = {
    id: `chatcmpl-chemrxn-browser-${turn}`,
    object: "chat.completion.chunk",
    created: Math.floor(Date.now() / 1000),
    model: body?.model || "chemrxn-browser-loopback",
  };

  response.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    connection: "keep-alive",
  });
  if (isFigureReviewFollowup && toolResults.length < 2) {
    const [name, args] = toolResults.length === 0
      ? ["open_chemistry_workspace", {
          module: "edit",
          reactionId: "example:0",
          focus: "selected-only",
        }]
      : ["propose_chemistry_correction", {
          reactionId: "example:0",
          rationale: "Compound 5 needs an explicit human-verification note without changing the sealed extraction.",
          evidence: ["Source figure bbox 3"],
          changes: [{
            kind: "molecule",
            side: "products",
            index: 0,
            field: "description",
            value: "Human verification requested for compound 5",
          }],
        }];
    for (const chunk of toolCall(base, name, args, toolResults.length + 1)) {
      response.write(`data: ${JSON.stringify(chunk)}\n\n`);
    }
    response.end("data: [DONE]\n\n");
    return;
  }
  if (isToolAcceptance && !isFigureReviewFollowup && toolResults.length < 3) {
    let name;
    let args;
    if (toolResults.length === 0) {
      const extractionPrompt = messages
        .filter((message) => message?.role === "user")
        .map(messageText)
        .find((text) => text.includes("/skill:reaction-extract")) || "";
      const imagePath = extractionPrompt.match(/`(input\/[^`]+)`/)?.[1];
      if (!imagePath) {
        response.write(`data: ${JSON.stringify({
          ...base,
          choices: [{
            index: 0,
            delta: { role: "assistant", content: "Scripted extraction input was unavailable." },
            finish_reason: "stop",
          }],
        })}\n\n`);
        response.end("data: [DONE]\n\n");
        return;
      }
      name = "detect";
      args = { image_path: imagePath, out_dir: "output/perception" };
    } else if (toolResults.length === 1) {
      name = "assemble";
      args = { plan: extractionPlan(), out_path: "output/result.v2.json" };
    } else {
      name = "render_reactions";
      args = {
        v2_path: "output/result.v2.json",
        image_path: messages
          .filter((message) => message?.role === "user")
          .map(messageText)
          .find((text) => text.includes("/skill:reaction-extract"))
          ?.match(/`(input\/[^`]+)`/)?.[1],
        out_html: "output/reactions.html",
      };
    }
    for (const chunk of toolCall(base, name, args, toolResults.length + 1)) {
      response.write(`data: ${JSON.stringify(chunk)}\n\n`);
    }
    response.end("data: [DONE]\n\n");
    return;
  }
  const finalAnswer = isFigureReviewFollowup
    ? "The correction is staged in the chemistry workspace for human review; it has not been applied."
    : isToolAcceptance
      ? "Live tool artifacts are sealed and ready in the chemistry workspace."
      : answer;
  response.write(`data: ${JSON.stringify({
    ...base,
    choices: [{
      index: 0,
      delta: { role: "assistant", content: finalAnswer },
      finish_reason: null,
    }],
  })}\n\n`);
  response.write(`data: ${JSON.stringify({
    ...base,
    choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
    usage: {
      prompt_tokens: 12,
      completion_tokens: 8,
      total_tokens: 20,
    },
  })}\n\n`);
  response.end("data: [DONE]\n\n");
});

server.listen(port, "127.0.0.1", () => {
  process.stdout.write(`ChemRxn Pi loopback listening on 127.0.0.1:${port}\n`);
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
