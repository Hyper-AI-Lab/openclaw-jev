import "./string-coerce-runtime-B1b8UNzb.mjs";
import { C as resolveOpenAICodexReasoningEfforts, _ as OPENAI_GPT_6_MODEL_IDS, a as OPENAI_GPT_54_MINI_MODEL_ID, c as OPENAI_GPT_54_PRO_MODEL_ID, f as OPENAI_GPT_56_MODEL_ID, l as OPENAI_GPT_55_MODEL_ID, o as OPENAI_GPT_54_MODEL_ID, r as OPENAI_GPT_53_CODEX_SPARK_MODEL_ID, s as OPENAI_GPT_54_NANO_MODEL_ID, u as OPENAI_GPT_55_PRO_MODEL_ID } from "./model-route-contract-B0eTJHEC.mjs";
import { t as modelCatalog } from "./openclaw.plugin-CcmnWH2C.mjs";

// ---- fixture cut ----

	const canSynthesizeUltra = thinkingLevelMap?.max !== null;
	if (OPENAI_GPT_6_MODEL_IDS.some((id) => id === modelId)) {
		const fallbackEfforts = modelCatalog.providers.openai.models.find((model) => model.id === modelId)?.compat?.supportedReasoningEfforts ?? [];
