const INHERITED_CHEMISTRY_ENV = [
  "PATH",
  "PYTHONPATH",
  "VIRTUAL_ENV",
  "CONDA_PREFIX",
  "JAVA_HOME",
  "HOME",
  "USER",
  "LOGNAME",
  "SHELL",
  "TMPDIR",
  "TMP",
  "TEMP",
  "LANG",
  "LC_ALL",
  "LC_CTYPE",
  "SSL_CERT_FILE",
  "SSL_CERT_DIR",
  "REQUESTS_CA_BUNDLE",
  "CURL_CA_BUNDLE",
  "DYLD_LIBRARY_PATH",
  "LD_LIBRARY_PATH",
  "OCSR_CACHE",
  "UNIPARSER_API_KEY",
  "UNIPARSER_BASE_URL",
  "UNIPARSER_HOST",
  "OPSIN_JAVA_BIN",
  "FHR_REVERSE",
  "MAX_TOKENS",
  "CHEMBENCH_REMOTE_NAME_LOOKUP",
] as const;

/**
 * Build the environment for local chemistry-only subprocesses.
 *
 * The Pi runtime process can hold subscription auth, a loopback bearer and
 * model-provider credentials. Chemistry workers need none of those. They get
 * only OS/runtime paths plus explicitly named chemistry services; proxy vars
 * are intentionally omitted so MolParser cannot silently inherit a proxy.
 */
export function chemistrySubprocessEnv(
  overrides: NodeJS.ProcessEnv = {},
  inherited: NodeJS.ProcessEnv = process.env,
): NodeJS.ProcessEnv {
  const result: NodeJS.ProcessEnv = {};
  for (const key of INHERITED_CHEMISTRY_ENV) {
    const value = inherited[key];
    if (value !== undefined) result[key] = value;
  }
  // Product extraction must use the same high-fidelity R-group path as the
  // direct Figure runner. Keep an explicit inherited/override value for
  // controlled diagnostics, but never silently downgrade normal MCP calls to
  // the legacy molzip/RGD fallback merely because the launcher omitted a flag.
  if (result.FHR_REVERSE === undefined) result.FHR_REVERSE = "1";
  for (const [key, value] of Object.entries(overrides)) {
    if (value === undefined || sensitiveRuntimeKey(key)) continue;
    result[key] = value;
  }
  return result;
}

export function pdfIngestSubprocessEnv(
  inherited: NodeJS.ProcessEnv = process.env,
): NodeJS.ProcessEnv {
  const result = chemistrySubprocessEnv({}, inherited);
  for (const key of ["UNIPARSER_API_KEY", "UNIPARSER_HOST", "UNIPARSER_TOOLS_DIR"] as const) {
    const value = inherited[key];
    if (value !== undefined) result[key] = value;
  }
  return result;
}

function sensitiveRuntimeKey(key: string): boolean {
  if (["UNIPARSER_API_KEY"].includes(key)) return false;
  return key.startsWith("CHEMRXN_PI_")
    || key.startsWith("PI_CODING_AGENT_")
    || /(?:^|_)(?:OPENAI|ANTHROPIC|GEMINI|GOOGLE|VERTEX)(?:_|$)/.test(key)
    || /(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)/.test(key);
}
