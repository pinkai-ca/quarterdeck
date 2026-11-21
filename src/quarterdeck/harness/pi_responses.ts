// Pi 0.79.0 uses model.maxTokens for model metadata, but does not pass it to
// the Responses SDK. Load this hook explicitly, with extension discovery off.
export default function (pi) {
  pi.on("before_provider_request", ({ payload }, { model }) => {
    if (model?.api !== "openai-responses"
        || model.provider !== process.env.QD_PI_RESPONSES_PROVIDER
        || model.baseUrl.replace(/\/$/, "") !== process.env.QD_PI_RESPONSES_BASE_URL
        || payload?.model !== model.id || !Array.isArray(payload.input)) {
      return;
    }
    const limit = payload.max_output_tokens ?? model.maxTokens;
    return { ...payload, max_output_tokens: Math.min(limit, model.maxTokens) };
  });
}
