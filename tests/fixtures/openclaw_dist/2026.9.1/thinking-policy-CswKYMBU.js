}
function buildOpenAIThinkingProfile(params) {
	const modelId = normalizeLowercaseStringOrEmpty(params.modelId);
	const agentRuntime = normalizeLowercaseStringOrEmpty(params.agentRuntime ?? "");
	const codexEfforts = params.compat?.supportedReasoningEfforts?.map(normalizeLowercaseStringOrEmpty);
	const resolvedCodexEfforts = params.api === void 0 || params.api === "openai-chatgpt-responses" ? resolveOpenAICodexReasoningEfforts(modelId, codexEfforts) : void 0;
	const knownCodexEfforts = resolveOpenAICodexReasoningEfforts(modelId, void 0);
	const isGpt56Variant = knownCodexEfforts !== void 0;
	const codexSupportsMax = (resolvedCodexEfforts ?? knownCodexEfforts)?.includes("max");
	const supportsMax = modelId.startsWith("gpt-5.6") && (agentRuntime !== "codex" || codexSupportsMax);
	const codexSupportsUltra = (resolvedCodexEfforts ?? knownCodexEfforts)?.includes("ultra");
	const supportsUltra = (modelId === "gpt-5.6" || isGpt56Variant) && (agentRuntime === "openclaw" || agentRuntime === "auto" || agentRuntime === "codex" && codexSupportsUltra);
	const nativeCodexNeedsAccountEffortValidation = agentRuntime === "codex" && params.compat?.supportedReasoningEfforts === void 0 && (params.api === void 0 || params.api === "openai-chatgpt-responses") && !matchesExactOrPrefix(params.modelId, params.xhighModelIds) && !modelId.startsWith("gpt-5.6");
	const defaultLevel = isGpt56Variant ? "medium" : void 0;
	const fallbackLevels = [
		...OPENAI_THINKING_BASE_LEVELS,
		...matchesExactOrPrefix(params.modelId, params.xhighModelIds) ? [{ id: "xhigh" }] : [],
		...supportsMax ? [{ id: "max" }] : [],
		...supportsUltra ? [{ id: "ultra" }] : [],
		...nativeCodexNeedsAccountEffortValidation ? [{ id: "xhigh" }, { id: "max" }] : []
	];
	const levels = agentRuntime === "codex" && resolvedCodexEfforts !== void 0 ? buildCodexLevels(resolvedCodexEfforts) : fallbackLevels;
	return {
		levels,
		...defaultLevel && levels.some((level) => level.id === defaultLevel) ? { defaultLevel } : {}
	};
}
