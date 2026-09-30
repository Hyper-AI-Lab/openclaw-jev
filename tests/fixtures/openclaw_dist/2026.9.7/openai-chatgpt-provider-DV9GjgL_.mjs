import { t as OPENAI_CODEX_DEFAULT_MODEL } from "./default-models-DJNhPIhI.mjs";
import { _ as OPENAI_GPT_6_MODEL_IDS, a as OPENAI_GPT_54_MINI_MODEL_ID, c as OPENAI_GPT_54_PRO_MODEL_ID, h as OPENAI_GPT_56_VARIANT_MODEL_IDS, l as OPENAI_GPT_55_MODEL_ID, o as OPENAI_GPT_54_MODEL_ID, r as OPENAI_GPT_53_CODEX_SPARK_MODEL_ID, t as OPENAI_CHATGPT_MODERN_MODEL_IDS, u as OPENAI_GPT_55_PRO_MODEL_ID } from "./model-route-contract-B0eTJHEC.mjs";
import { t as modelCatalog } from "./openclaw.plugin-CcmnWH2C.mjs";

// ---- fixture cut ----

const OPENAI_CODEX_IMAGE_CAPABLE_MODEL_IDS = [
	...OPENAI_GPT_6_MODEL_IDS,
	...OPENAI_GPT_56_VARIANT_MODEL_IDS,

// ---- fixture cut ----

	const synthBaseUrl = ctx.providerConfig?.baseUrl ?? "https://chatgpt.com/backend-api/codex";
	if (OPENAI_GPT_6_MODEL_IDS.some((modelId) => modelId === lower)) {
		const catalogModel = OPENAI_MANIFEST_MODELS.find((model) => model.id === lower);
