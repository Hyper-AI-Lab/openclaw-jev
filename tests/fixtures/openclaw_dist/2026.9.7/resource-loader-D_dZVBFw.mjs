	const beforeMessageWrite = (event, sourceAppend) => {
		if (isMidTurnPrecheckAssistantError(event.message)) return { block: true };
		const runtimeUserMessage = runtimeUserMessageByPersistedMessage.get(event.message);
		let message = event.message;
		let changed = false;
		const skipUserWriteHook = skipBeforeMessageWriteHooks || message.role === "user" && queuedUserTurnTranscriptRecorder?.getPendingInputMessage?.() !== void 0;
		if (!skipUserWriteHook && hookRunner?.hasHooks("before_message_write") || prepareAssistantTranscriptMessage) {
			const preparedMessage = message.role === "user" ? {
				...message,
				__openclaw: { ...Reflect.get(message, "__openclaw") }
			} : void 0;
			if (preparedMessage && (preparedMessage["__openclaw"].humanMentions !== void 0 || preparedMessage["__openclaw"].workContext !== void 0)) {
				preparedMessage.content = structuredClone(preparedMessage.content);
