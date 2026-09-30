import { a as OPENAI_DEFAULT_MODEL, c as applyOpenAIConfig, t as OPENAI_CODEX_DEFAULT_MODEL } from "./default-models-DJNhPIhI.mjs";
import { C as resolveOpenAICodexReasoningEfforts, S as normalizeOpenAIModelRouteId, _ as OPENAI_GPT_6_MODEL_IDS, a as OPENAI_GPT_54_MINI_MODEL_ID, b as isOpenAIPlatformOnlyRouteModelId, c as OPENAI_GPT_54_PRO_MODEL_ID, d as OPENAI_GPT_56_LUNA_MODEL_ID, f as OPENAI_GPT_56_MODEL_ID, l as OPENAI_GPT_55_MODEL_ID, m as OPENAI_GPT_56_TERRA_MODEL_ID, n as OPENAI_CHAT_LATEST_MODEL_ID, o as OPENAI_GPT_54_MODEL_ID, p as OPENAI_GPT_56_SOL_MODEL_ID, s as OPENAI_GPT_54_NANO_MODEL_ID, u as OPENAI_GPT_55_PRO_MODEL_ID, v as OPENAI_PROVIDER_MODERN_MODEL_IDS, x as isOpenAISubscriptionOnlyRouteModelId } from "./model-route-contract-B0eTJHEC.mjs";
import { t as modelCatalog } from "./openclaw.plugin-CcmnWH2C.mjs";

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
