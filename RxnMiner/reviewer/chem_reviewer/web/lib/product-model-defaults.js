export const PRODUCT_API_PROVIDER_OPTIONS = Object.freeze([
  Object.freeze({
    registryId: "byo-openai",
    piProvider: "openai",
    label: "OpenAI",
    defaultModel: "gpt-5.6-terra",
  }),
  Object.freeze({
    registryId: "byo-anthropic",
    piProvider: "anthropic",
    label: "Anthropic",
    defaultModel: "claude-sonnet-4-6",
  }),
  Object.freeze({
    registryId: "byo-gemini",
    piProvider: "google",
    label: "Google Gemini",
    defaultModel: "gemini-3.1-pro-preview",
  }),
]);

const PRODUCT_API_PROVIDER_BY_ID = new Map(
  PRODUCT_API_PROVIDER_OPTIONS.map((option) => [option.registryId, option]),
);

export function productApiProvider(registryId) {
  return PRODUCT_API_PROVIDER_BY_ID.get(registryId);
}

export function defaultProductApiModel(registryId) {
  return productApiProvider(registryId)?.defaultModel || "";
}
