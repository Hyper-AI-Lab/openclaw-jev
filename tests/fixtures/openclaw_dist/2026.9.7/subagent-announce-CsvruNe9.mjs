		result = stripSilentToken(result, SILENT_REPLY_TOKEN);
		didStrip = true;
	}
	if (didStrip && (!result.trim() || isSilentReplyText(result, "NO_REPLY") || isAnnounceSkip(result))) return null;
	return result;
}
async function runSubagentAnnounceFlow(params) {
	return await (params.resolveGatewayContext ? withPluginRuntimeGatewayContextResolver(params.resolveGatewayContext, () => runSubagentAnnounceFlowBound(params)) : runSubagentAnnounceFlowBound(params));
}
async function runSubagentAnnounceFlowBound(params) {
	let announceOutcome = "retryable";
	const expectsCompletionMessage = params.expectsCompletionMessage === true;
	const announceType = params.announceType ?? "subagent task";
