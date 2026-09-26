import { EventEmitter } from "node:events";
import * as undici from "undici";

export type ModelNetworkMode = "environment_proxy" | "direct";

/**
 * Match Pi CLI's network bootstrap for the embedded SDK path. ModelRuntime and
 * createAgentSession do not install Pi CLI's dispatcher themselves.
 *
 * EnvHttpProxyAgent honors HTTP_PROXY / HTTPS_PROXY / NO_PROXY. It affects only
 * this Node runtime; whole-page MolParser remains a separate Python call whose
 * implementation explicitly strips proxy variables.
 */
export function configureModelNetwork(
  env: NodeJS.ProcessEnv = process.env,
  installGlobals = true,
): ModelNetworkMode {
  const httpProxy = firstEnvironmentValue(env.http_proxy, env.HTTP_PROXY);
  const httpsProxy = firstEnvironmentValue(env.https_proxy, env.HTTPS_PROXY);
  const noProxy = firstEnvironmentValue(env.no_proxy, env.NO_PROXY);
  const dispatcher = new undici.EnvHttpProxyAgent({
    allowH2: false,
    bodyTimeout: 300_000,
    headersTimeout: 300_000,
    // Explicit empty values prevent an injected test/tenant environment from
    // silently falling back to this process's unrelated proxy configuration.
    httpProxy: httpProxy || "",
    httpsProxy: httpsProxy || "",
    noProxy: noProxy || "",
  });
  if (dispatcher instanceof EventEmitter) {
    EventEmitter.prototype.on.call(dispatcher, "error", () => {});
  }
  undici.setGlobalDispatcher(dispatcher);
  if (installGlobals) undici.install();
  return hasHttpProxy(env) ? "environment_proxy" : "direct";
}

function hasHttpProxy(env: NodeJS.ProcessEnv): boolean {
  return Boolean(
    firstEnvironmentValue(env.http_proxy, env.HTTP_PROXY)
    || firstEnvironmentValue(env.https_proxy, env.HTTPS_PROXY),
  );
}

function firstEnvironmentValue(...values: Array<string | undefined>): string | undefined {
  return values.find((value) => typeof value === "string" && value.trim())?.trim();
}
