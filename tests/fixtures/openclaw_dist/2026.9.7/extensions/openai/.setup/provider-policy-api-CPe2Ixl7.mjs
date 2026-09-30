const OPENAI_CHAT_LATEST_MODEL_ID = "chat-latest";
const OPENAI_GPT_6_MODEL_IDS = [
	"gpt-6-astra",

// ---- fixture cut ----

const OPENAI_DUAL_ROUTE_MODEL_IDS = [
	...OPENAI_GPT_6_MODEL_IDS,
	...OPENAI_GPT_56_VARIANT_MODEL_IDS,

// ---- fixture cut ----

	const canSynthesizeUltra = thinkingLevelMap?.max !== null;
	if (OPENAI_GPT_6_MODEL_IDS.some((id) => id === modelId)) {
		const fallbackEfforts = modelCatalog.providers.openai.models.find((model) => model.id === modelId)?.compat?.supportedReasoningEfforts ?? [];

// ---- fixture cut ----

//#endregion
export { isOpenAIPlatformOnlyRouteModelId as A, OPENAI_GPT_56_LUNA_MODEL_ID as C, OPENAI_GPT_56_VARIANT_MODEL_IDS as D, OPENAI_GPT_56_TERRA_MODEL_ID as E, classifyOpenAIBaseUrl as F, isOpenAIApiBaseUrl as I, isOpenAICodexBaseUrl as L, normalizeOpenAIModelRouteId as M, resolveOpenAICodexReasoningEfforts as N, OPENAI_GPT_6_MODEL_IDS as O, OPENAI_CODEX_RESPONSES_BASE_URL as P, resolveOpenAIDefaultBaseUrl as R, OPENAI_GPT_55_PRO_MODEL_ID as S, OPENAI_GPT_56_SOL_MODEL_ID as T, OPENAI_GPT_54_MINI_MODEL_ID as _, modelCatalog as a, OPENAI_GPT_54_PRO_MODEL_ID as b, isOpenAIGptLiveApiModel as c, resolveOpenAIQuicksilverVoice as d, resolveOpenAIQuicksilverVoiceCapabilities as f, OPENAI_GPT_54_LEGACY_MODEL_ID as g, OPENAI_GPT_53_CODEX_SPARK_MODEL_ID as h, resolveUnifiedOpenAIThinkingProfile as i, isOpenAISubscriptionOnlyRouteModelId as j, OPENAI_PROVIDER_MODERN_MODEL_IDS as k, isOpenAIGptLiveModel as l, OPENAI_CHAT_LATEST_MODEL_ID as m, resolveAuthoredOpenAIProviderConfig as n, OPENAI_GPT_LIVE_MODELS as o, OPENAI_CHATGPT_MODERN_MODEL_IDS as p, resolveNativeWebSearch as r, OPENAI_QUICKSILVER_CAPABILITIES as s, projectRealtimeVoicePublicProjection as t, isOpenAIGptLiveSubscriptionModel as u, OPENAI_GPT_54_MODEL_ID as v, OPENAI_GPT_56_MODEL_ID as w, OPENAI_GPT_55_MODEL_ID as x, OPENAI_GPT_54_NANO_MODEL_ID as y };

