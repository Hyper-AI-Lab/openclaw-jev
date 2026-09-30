}
function normalizeAgentPayload(payload) {
	const message = normalizeOptionalString(payload.message) ?? "";
	if (!message) return {
		ok: false,
		error: "message required"
	};
	const name = normalizeOptionalString(payload.name) ?? "Hook";
	const agentId = normalizeHookPayloadAgentId(payload.agentId);
	if (!agentId.ok) return agentId;
	const idempotencyKey = normalizeBoundedOptionalString(payload.idempotencyKey, MAX_HOOK_IDEMPOTENCY_KEY_LENGTH);
	const wakeMode = payload.wakeMode === "next-heartbeat" ? "next-heartbeat" : "now";
	const sessionKey = normalizeOptionalString(payload.sessionKey);
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
	const thinking = normalizeOptionalString(payload.thinking);
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
