import { A as isOpenAIPlatformOnlyRouteModelId, C as OPENAI_GPT_56_LUNA_MODEL_ID, D as OPENAI_GPT_56_VARIANT_MODEL_IDS, E as OPENAI_GPT_56_TERRA_MODEL_ID, F as classifyOpenAIBaseUrl, I as isOpenAIApiBaseUrl, L as isOpenAICodexBaseUrl, M as normalizeOpenAIModelRouteId, N as resolveOpenAICodexReasoningEfforts, O as OPENAI_GPT_6_MODEL_IDS, P as OPENAI_CODEX_RESPONSES_BASE_URL, R as resolveOpenAIDefaultBaseUrl, S as OPENAI_GPT_55_PRO_MODEL_ID, T as OPENAI_GPT_56_SOL_MODEL_ID, _ as OPENAI_GPT_54_MINI_MODEL_ID, a as modelCatalog, b as OPENAI_GPT_54_PRO_MODEL_ID, h as OPENAI_GPT_53_CODEX_SPARK_MODEL_ID, i as resolveUnifiedOpenAIThinkingProfile, j as isOpenAISubscriptionOnlyRouteModelId, k as OPENAI_PROVIDER_MODERN_MODEL_IDS, m as OPENAI_CHAT_LATEST_MODEL_ID, n as resolveAuthoredOpenAIProviderConfig, p as OPENAI_CHATGPT_MODERN_MODEL_IDS, v as OPENAI_GPT_54_MODEL_ID, w as OPENAI_GPT_56_MODEL_ID, x as OPENAI_GPT_55_MODEL_ID, y as OPENAI_GPT_54_NANO_MODEL_ID } from "./provider-policy-api-CPe2Ixl7.mjs";
import { l as isSIWCAuthFlow, s as TOKEN_SHARING_RESOURCE } from "./token-sharing-CmBELw8t.mjs";

// ---- fixture cut ----

const OPENAI_CODEX_IMAGE_CAPABLE_MODEL_IDS = [
	...OPENAI_GPT_6_MODEL_IDS,
	...OPENAI_GPT_56_VARIANT_MODEL_IDS,

// ---- fixture cut ----

	const synthBaseUrl = ctx.providerConfig?.baseUrl ?? "https://chatgpt.com/backend-api/codex";
	if (OPENAI_GPT_6_MODEL_IDS.some((modelId) => modelId === lower)) {
		const catalogModel = OPENAI_MANIFEST_MODELS.find((model) => model.id === lower);

// ---- fixture cut ----

			if (isOpenAIPlatformOnlyRouteModelId(modelId)) return [];
			if (OPENAI_GPT_6_MODEL_IDS.some((id) => id === modelId) || modelId.startsWith("gpt-5.6") && modelId !== "gpt-5.6-sol") return [];
			return [normalizeOpenAICodexCatalogModel(model)];

// ---- fixture cut ----

	{
		match: OPENAI_GPT_6_MODEL_IDS,
		templateIds: [OPENAI_GPT_56_SOL_MODEL_ID, OPENAI_GPT_55_MODEL_ID]

// ---- fixture cut ----

	const exactModel = ctx.modelRegistry.find(PROVIDER_ID, trimmedModelId);
	if (OPENAI_GPT_6_MODEL_IDS.some((id) => id === modelId) || modelId === "gpt-5.6-sol" || modelId === "gpt-5.6-terra" || modelId === "gpt-5.6-luna") {
		if (exactModel) return exactModel;
