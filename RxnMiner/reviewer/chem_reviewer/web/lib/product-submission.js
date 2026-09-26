export function snapshotProductSubmission(state) {
  return Object.freeze({
    authMode: state.authMode,
    apiProvider: state.apiProvider,
    apiModel: state.apiModel.trim(),
    thinkingLevel: state.thinkingLevel,
    apiKey: state.authMode === "api_key" ? state.apiKey : "",
    sourceType: state.sourceType,
    primary: state.primary,
    si: state.si,
  });
}

export function productSubmissionModelInput(submission) {
  return Object.freeze({
    provider: submission.apiProvider,
    modelId: submission.apiModel,
    requireImage: submission.sourceType === "figure",
  });
}

export function buildProductExtractionInput(submission) {
  if (submission.sourceType !== "figure" || !submission.primary || submission.si) throw new Error("Choose one reaction image");
  const form = new FormData();
  form.append("source_type", submission.sourceType);
  form.append("primary", submission.primary, submission.primary.name);
  form.append("auth_mode", submission.authMode);
  form.append("thinking_level", submission.thinkingLevel);
  if (submission.authMode === "api_key") {
    form.append("llm_provider", submission.apiProvider);
    form.append("llm_model", submission.apiModel);
    form.append("llm_key", submission.apiKey);
  }
  return form;
}
