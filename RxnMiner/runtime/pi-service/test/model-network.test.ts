import assert from "node:assert/strict";
import { createServer, type Server } from "node:http";
import { once } from "node:events";
import { connect } from "node:net";
import { test } from "node:test";
import {
  EnvHttpProxyAgent,
  fetch,
  getGlobalDispatcher,
  setGlobalDispatcher,
} from "undici";
import { configureModelNetwork } from "../src/model-network.js";

test("embedded Pi SDK routes model traffic through the supplied environment proxy", async () => {
  let targetRequests = 0;
  let proxyConnections = 0;
  const target = createServer((_request, response) => {
    targetRequests += 1;
    response.writeHead(204).end();
  });
  const proxy = createServer((_request, response) => response.writeHead(502).end());
  proxy.on("connect", (request, client, head) => {
    proxyConnections += 1;
    const [host, port] = (request.url || "").split(":");
    const upstream = connect(Number(port), host, () => {
      client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      if (head.length) upstream.write(head);
      upstream.pipe(client);
      client.pipe(upstream);
    });
    upstream.on("error", () => client.destroy());
  });
  await Promise.all([listen(target), listen(proxy)]);
  const previous = getGlobalDispatcher();
  let dispatcher: ReturnType<typeof getGlobalDispatcher> | undefined;
  try {
    const address = proxy.address();
    const targetAddress = target.address();
    assert.ok(address && typeof address === "object");
    assert.ok(targetAddress && typeof targetAddress === "object");
    const mode = configureModelNetwork({
      HTTPS_PROXY: `http://127.0.0.1:${address.port}`,
      HTTP_PROXY: `http://127.0.0.1:${address.port}`,
      NO_PROXY: "example.invalid",
    }, false);
    dispatcher = getGlobalDispatcher();
    assert.equal(mode, "environment_proxy");
    assert.ok(dispatcher instanceof EnvHttpProxyAgent);
    const response = await fetch(`http://127.0.0.1:${targetAddress.port}/model-probe`);
    assert.equal(response.status, 204);
    assert.equal(proxyConnections, 1);
    assert.equal(targetRequests, 1);
  } finally {
    setGlobalDispatcher(previous);
    await dispatcher?.close();
    await Promise.all([close(target), close(proxy)]);
  }
});

test("embedded Pi SDK honors supplied NO_PROXY for loopback services", async () => {
  let directRequests = 0;
  let proxyRequests = 0;
  const target = createServer((_request, response) => {
    directRequests += 1;
    response.writeHead(200, { "content-type": "text/plain" }).end("direct");
  });
  const proxy = createServer((_request, response) => {
    proxyRequests += 1;
    response.writeHead(502).end();
  });
  await Promise.all([listen(target), listen(proxy)]);
  const previous = getGlobalDispatcher();
  let dispatcher: ReturnType<typeof getGlobalDispatcher> | undefined;
  try {
    const targetAddress = target.address();
    const proxyAddress = proxy.address();
    assert.ok(targetAddress && typeof targetAddress === "object");
    assert.ok(proxyAddress && typeof proxyAddress === "object");
    const mode = configureModelNetwork({
      HTTP_PROXY: `http://127.0.0.1:${proxyAddress.port}`,
      NO_PROXY: "127.0.0.1",
    }, false);
    dispatcher = getGlobalDispatcher();
    assert.equal(mode, "environment_proxy");
    const response = await fetch(`http://127.0.0.1:${targetAddress.port}/healthz`);
    assert.equal(await response.text(), "direct");
    assert.equal(directRequests, 1);
    assert.equal(proxyRequests, 0);
  } finally {
    setGlobalDispatcher(previous);
    await dispatcher?.close();
    await Promise.all([close(target), close(proxy)]);
  }
});

test("embedded Pi SDK reports a direct environment without leaking configuration", async () => {
  let directRequests = 0;
  let inheritedProxyRequests = 0;
  const target = createServer((_request, response) => {
    directRequests += 1;
    response.writeHead(200).end("direct");
  });
  const inheritedProxy = createServer((_request, response) => {
    inheritedProxyRequests += 1;
    response.writeHead(502).end();
  });
  inheritedProxy.on("connect", (_request, client) => {
    inheritedProxyRequests += 1;
    client.write("HTTP/1.1 502 Bad Gateway\r\n\r\n");
    client.destroy();
  });
  await Promise.all([listen(target), listen(inheritedProxy)]);
  const previous = getGlobalDispatcher();
  let dispatcher: ReturnType<typeof getGlobalDispatcher> | undefined;
  const inheritedHttpProxy = process.env.HTTP_PROXY;
  const inheritedHttpsProxy = process.env.HTTPS_PROXY;
  const inheritedNoProxy = process.env.NO_PROXY;
  try {
    const targetAddress = target.address();
    const proxyAddress = inheritedProxy.address();
    assert.ok(targetAddress && typeof targetAddress === "object");
    assert.ok(proxyAddress && typeof proxyAddress === "object");
    process.env.HTTP_PROXY = `http://127.0.0.1:${proxyAddress.port}`;
    process.env.HTTPS_PROXY = `http://127.0.0.1:${proxyAddress.port}`;
    process.env.NO_PROXY = "";
    assert.equal(configureModelNetwork({}, false), "direct");
    dispatcher = getGlobalDispatcher();
    assert.ok(dispatcher instanceof EnvHttpProxyAgent);
    const response = await fetch(`http://127.0.0.1:${targetAddress.port}/direct`);
    assert.equal(await response.text(), "direct");
    assert.equal(directRequests, 1);
    assert.equal(inheritedProxyRequests, 0);
  } finally {
    restoreEnvironment("HTTP_PROXY", inheritedHttpProxy);
    restoreEnvironment("HTTPS_PROXY", inheritedHttpsProxy);
    restoreEnvironment("NO_PROXY", inheritedNoProxy);
    setGlobalDispatcher(previous);
    await dispatcher?.close();
    await Promise.all([close(target), close(inheritedProxy)]);
  }
});

async function listen(server: Server): Promise<void> {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
}

async function close(server: Server): Promise<void> {
  if (!server.listening) return;
  server.close();
  await once(server, "close");
}

function restoreEnvironment(name: string, value: string | undefined): void {
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}
