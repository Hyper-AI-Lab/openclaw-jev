		result = stripSilentToken(result, SILENT_REPLY_TOKEN);
		didStrip = true;
	}
	if (didStrip && (!result.trim() || isSilentReplyText(result, "NO_REPLY") || isAnnounceSkip(result))) return null;
	return result;
}
async function runSubagentAnnounceFlow(params) {
	let announceOutcome = "retryable";
	const expectsCompletionMessage = params.expectsCompletionMessage === true;
	const announceType = params.announceType ?? "subagent task";
	let shouldDeleteChildSession = params.cleanup === "delete";
	const childSessionEffectsAllowed = () => params.suppressChildSessionEffects !== true && params.isChildSessionEffectsAllowed?.() !== false;
	const completionDeliveryAllowed = () => params.isCompletionDeliveryAllowed?.() !== false;
