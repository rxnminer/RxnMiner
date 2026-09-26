import assert from "node:assert/strict";
import { test } from "node:test";
import {
  chemistrySubprocessEnv,
  pdfIngestSubprocessEnv,
} from "../src/subprocess-env.js";

test("chemistry subprocesses inherit tools but not runtime, model, or proxy secrets", () => {
  const inherited = {
    PATH: "/usr/local/bin:/usr/bin",
    JAVA_HOME: "/opt/java",
    OPSIN_JAVA_BIN: "/opt/opsin/bin/java",
    MOLPARSER_URL: "http://molparser.invalid/extract",
    UNIPARSER_API_KEY: "uniparser-service-secret",
    UNIPARSER_HOST: "https://uniparser.invalid/",
    CHEMRXN_PI_INTERNAL_TOKEN: "loopback-bearer-secret",
    CHEMRXN_PI_AUTH_PATH: "/private/pi-auth.json",
    OPENAI_API_KEY: "model-provider-secret",
    ANTHROPIC_API_KEY: "other-model-secret",
    HTTP_PROXY: "http://proxy.invalid",
    HTTPS_PROXY: "http://proxy.invalid",
  } satisfies NodeJS.ProcessEnv;

  const mcp = chemistrySubprocessEnv({
    PID_FILE: "/tmp/chemrxn-test.pid",
    CHEMRXN_PI_INTERNAL_TOKEN: "must-still-be-dropped",
  }, inherited);
  assert.equal(mcp.PATH, inherited.PATH);
  assert.equal(mcp.OPSIN_JAVA_BIN, inherited.OPSIN_JAVA_BIN);
  assert.equal(mcp.MOLPARSER_URL, undefined);
  assert.equal(mcp.UNIPARSER_HOST, inherited.UNIPARSER_HOST);
  assert.equal(mcp.PID_FILE, "/tmp/chemrxn-test.pid");
  assert.equal(mcp.FHR_REVERSE, "1", "Product chemistry defaults to the high-fidelity FHR path");
  assert.equal(
    chemistrySubprocessEnv({}, { ...inherited, FHR_REVERSE: "0" }).FHR_REVERSE,
    "0",
    "controlled diagnostics may explicitly disable FHR",
  );
  assert.equal(mcp.UNIPARSER_API_KEY, inherited.UNIPARSER_API_KEY, "Molecule recognition uses the configured UniParser account");
  for (const name of [
    "CHEMRXN_PI_INTERNAL_TOKEN",
    "CHEMRXN_PI_AUTH_PATH",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
  ]) assert.equal(mcp[name], undefined, `${name} leaked into chemistry MCP`);

  const ingest = pdfIngestSubprocessEnv(inherited);
  assert.equal(ingest.UNIPARSER_API_KEY, inherited.UNIPARSER_API_KEY);
  assert.equal(ingest.UNIPARSER_HOST, inherited.UNIPARSER_HOST);
  assert.equal(ingest.OPSIN_JAVA_BIN, inherited.OPSIN_JAVA_BIN);
  assert.equal(ingest.CHEMRXN_PI_INTERNAL_TOKEN, undefined);
  assert.equal(ingest.OPENAI_API_KEY, undefined);
  assert.equal(ingest.HTTP_PROXY, undefined, "MolParser must not inherit a proxy");
});
