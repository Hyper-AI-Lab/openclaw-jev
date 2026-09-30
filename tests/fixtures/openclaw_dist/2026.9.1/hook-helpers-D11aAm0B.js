	}
}
/** Runs before-message-write hooks and returns the possibly rewritten message. */
function runAgentHarnessBeforeMessageWriteHook(params) {
	const sourceText = params.prepareAssistantTranscriptMessage && params.message.role === "assistant" && Reflect.get(params.message, "display") !== false ? extractAssistantPhaseText(params.message) : void 0;
	const hookRunner = getGlobalHookRunner();
	const message = !params.skipBeforeMessageWriteHooks && hookRunner?.hasHooks("before_message_write") ? applyTranscriptSenderIdentityToWrite(params.message, () => {
		const result = hookRunner.runBeforeMessageWrite({ message: params.message }, {
			agentId: params.agentId,
			sessionKey: params.sessionKey
		});
		return result?.block ? null : result?.message ?? params.message;
	}) ?? null : params.message;
