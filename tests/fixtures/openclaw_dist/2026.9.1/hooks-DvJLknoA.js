/** Validate and normalize a hook agent payload before policy/session resolution. */
function normalizeAgentPayload(payload) {
	const message = normalizeOptionalString(payload.message) ?? "";
	if (!message) return {
		ok: false,
		error: "message required"
	};
	const nameRaw = payload.name;
	const name = normalizeOptionalString(nameRaw) ?? "Hook";
	const agentId = normalizeHookPayloadAgentId(payload.agentId);
	if (!agentId.ok) return agentId;
	const idempotencyKey = resolveOptionalHookIdempotencyKey(payload.idempotencyKey);
	const wakeMode = payload.wakeMode === "next-heartbeat" ? "next-heartbeat" : "now";
	const sessionKeyRaw = payload.sessionKey;
	const sessionKey = normalizeOptionalString(sessionKeyRaw);
	const sessionModeRaw = payload.sessionMode;
	if (sessionModeRaw !== void 0 && sessionModeRaw !== "isolated" && sessionModeRaw !== "persistent") return {
		ok: false,
		error: "sessionMode must be isolated or persistent"
	};
	const sessionMode = sessionModeRaw ?? "isolated";
	const delivery = normalizeHookAgentDelivery({
		deliver: payload.deliver,
		channel: payload.channel,
		to: payload.to,
		accountId: payload.accountId
	});
	if (!delivery.ok) return delivery;
	const modelRaw = payload.model;
	const model = normalizeOptionalString(modelRaw);
	if (modelRaw !== void 0 && !model) return {
		ok: false,
		error: "model required"
	};
	const thinkingRaw = payload.thinking;
	const thinking = normalizeOptionalString(thinkingRaw);
	const timeoutRaw = payload.timeoutSeconds;
	const timeoutSeconds = typeof timeoutRaw === "number" && Number.isFinite(timeoutRaw) && timeoutRaw > 0 ? Math.floor(timeoutRaw) : void 0;
	return {
		ok: true,
		value: {
			message,
			name,
			agentId: agentId.value,
			idempotencyKey,
			wakeMode,
			sessionKey,
			sessionMode,
			...delivery.value,
			model,
			thinking,
			timeoutSeconds
		}
	};
}
