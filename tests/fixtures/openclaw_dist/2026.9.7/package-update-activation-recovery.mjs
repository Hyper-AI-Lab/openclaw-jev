}
function parseSqliteSessionEntryRecord(row) {
	try {
		const record = JSON.parse(row.entry_json);
		if (!isRecord$3(record)) return null;
		if (!hasValidSessionEntryIdentity(record)) return null;
		if (row.current_session_id !== void 0 && row.current_session_id !== record.sessionId || row.updated_at !== void 0 && row.updated_at !== record.updatedAt) return null;
		return record;
	} catch {
		return null;
	}
}

// ---- fixture cut ----

/** One validator serves full Doctor scans, pending rows, and final writer certification. */
function validateCanonicalSessionRow(row, mode = "admission") {
	const record = row.entry_valid === 1 || mode === "read" && row.entry_valid === 0 ? parseSqliteSessionEntryRecord({
		entry_json: row.entry_json,
		current_session_id: row.current_session_id
	}) : null;
	return validateCanonicalSessionRowEntry(row, record ? projectCanonicalSessionEntryShape(record) : null, mode);
}
/** Exact readers validate the entry decoded from the same selected row. */
function validateCanonicalSessionRowEntry(row, entry, mode = "admission") {
	if (row.entry_json === "{}" && row.entry_valid === -1 && row.retained_window_id === row.current_session_id) return;
	if (!entry || row.entry_valid !== 1 && (mode !== "read" || row.entry_valid !== 0)) throw canonicalSessionKeyMigrationRequiredError(`invalid persisted session row requires repair for ${row.session_key}`);
	if ((row.parent_session_key ?? void 0) !== (entry.parentSessionKey ?? entry.spawnedBy ?? void 0) || (row.spawned_by ?? void 0) !== (entry.spawnedBy ?? void 0) || (row.fork_source_session_key ?? void 0) !== (entry.forkSource?.sessionKey ?? void 0)) throw canonicalSessionKeyMigrationRequiredError(`invalid persisted session row requires repair for ${row.session_key}`);
	const deliveryCanonicalKey = resolveDeliveryProvenCanonicalSessionKey(row.session_key, entry);
	if (deliveryCanonicalKey !== row.session_key) throw canonicalSessionKeyMigrationRequiredError(`non-canonical persisted row resolves to session key ${deliveryCanonicalKey}`);
	const trimmed = row.session_key.trim();

// ---- fixture cut ----

	}
	if (status === 401 || status === 403) {
		if (opts?.preserveProviderSignalClassification && messageClassification) return messageClassification;
		if (message && isAuthPermanentErrorMessage(message)) return toReasonClassification("auth_permanent");
		if (messageReason === "billing") return toReasonClassification("billing");
		return toReasonClassification("auth");
	}
	if (status === 408) return toReasonClassification("timeout");
	if (status === 410) {
		if (messageReason === "session_expired" || messageReason === "billing" || messageReason === "auth_permanent" || messageReason === "auth") return messageClassification;
		return toReasonClassification("timeout");
	}
	if (messageClassification?.kind === "context_overflow") return messageClassification;
	if (status === 404) {
		if (messageReason === "session_expired" || messageReason === "billing" || messageReason === "auth_permanent" || messageReason === "auth" || messageReason === "format") return messageClassification;
		return toReasonClassification("model_not_found");
	}
	if (status === 529) return toReasonClassification("overloaded");

// ---- fixture cut ----

				continue;
			}
			if (resolveDeliveryProvenCanonicalSessionKey(candidate.session_key, entry) !== candidate.session_key) throw canonicalSessionKeyMigrationRequiredError(`non-canonical persisted row resolves to session key ${candidate.session_key}`);
		}
		const existing = candidates.find((candidate) => candidate.session_key === scope.sessionKey);
		nodeExists = existing !== void 0;
		if (existing && existing.entry_valid !== 1) {
			if (!(existing.entry_json === "{}" ? executeSqliteQueryTakeFirstSync(database.db, db.selectFrom("session_windows").select("session_id").where("session_id", "=", existing.current_session_id).where("session_key", "=", scope.sessionKey)) : void 0)) throw canonicalSessionKeyMigrationRequiredError(`invalid persisted session row requires repair for ${scope.sessionKey}`);
		}
	}
	if (!nodeExists) {
		if ((executeSqliteQuerySync(database.db, db.insertInto("session_nodes").values({
			session_key: scope.sessionKey,

// ---- fixture cut ----

		if (typeof file.path !== "string") return true;
		const filePath = file.path.trim();
		if (!filePath) return true;
		return resolveWorkspaceBootstrapPath(resolvedWorkspaceRoot, filePath) !== rootMemoryPath;
	});
}
function filterBootstrapFilesForSession(files, session) {
	const { sessionKey, chatType, workspaceDir } = resolveBootstrapSessionContext(session);
	const isSubagent = isSubagentSessionKey(sessionKey);
	const isCron = isCronSessionKey(sessionKey);
	const effectiveChatType = chatType ?? deriveSessionChatTypeFromKey(sessionKey);
	const privacyFilteredFiles = isSubagent || isCron || effectiveChatType === "group" || effectiveChatType === "channel" ? filterRootMemoryBootstrapFiles(files, workspaceDir) : files;
	if (isSubagent) return privacyFilteredFiles.filter((file) => SUBAGENT_BOOTSTRAP_ALLOWLIST.has(file.name));

// ---- fixture cut ----

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

// ---- fixture cut ----

	}
}
/** Runs before-message-write hooks and returns the possibly rewritten message. */
function runAgentHarnessBeforeMessageWriteHook(params) {
	const sourceText = params.prepareAssistantTranscriptMessage && params.message.role === "assistant" && Reflect.get(params.message, "display") !== false ? extractAssistantTranscriptSourceText(params.message) : void 0;
	const hookRunner = getGlobalHookRunner();
	const message = !params.skipBeforeMessageWriteHooks && hookRunner?.hasHooks("before_message_write") ? applyTranscriptSenderIdentityToWrite(params.message, () => {
		const result = hookRunner.runBeforeMessageWrite({ message: params.message }, {
			agentId: params.agentId,
			sessionKey: params.sessionKey
		});
		return result?.block ? null : result?.message ?? params.message;
	}) ?? null : params.message;

// ---- fixture cut ----

function shouldStripOpenAICompletionMessageKeys(model) {
	const compat = model.compat && typeof model.compat === "object" ? model.compat : void 0;
	return model.api === "openai-completions" && compat?.strictMessageKeys === true;
}
/** @deprecated OpenAI provider-owned stream helper; do not use from third-party plugins. */
function createOpenAIResponsesContextManagementWrapper(baseStreamFn, extraParams) {
	const underlying = baseStreamFn ?? streamSimple;
	return (model, context, options) => {
		const policy = resolveOpenAIResponsesPayloadPolicy(model, {
			extraParams,
			enablePromptCacheStripping: true,
			enableServerCompaction: true,
			storeMode: "provider-policy"
		});
		if (policy.explicitStore === void 0 && !policy.useServerCompaction && !policy.shouldStripStore && !policy.shouldStripPromptCache && !policy.shouldStripDisabledReasoningPayload) return underlying(model, context, options);
		const replayResponsesItemIds = (policy.shouldStripStore ? false : policy.explicitStore) ?? options?.replayResponsesItemIds;
		const nextOptions = {
			...options,

// ---- fixture cut ----

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

// ---- fixture cut ----

}
function resolveLlmFirstEventTimeoutMs(params) {
	const { timeoutBounds } = resolveLlmTimeoutBounds(params);
	const { isLocalRuntimeModel, isSelfHostedRuntimeModel } = resolveRuntimeModelLocality(params);
	const modelRequestTimeoutMs = params?.modelRequestTimeoutMs;
	if (typeof modelRequestTimeoutMs === "number" && Number.isFinite(modelRequestTimeoutMs) && modelRequestTimeoutMs > 0) return clampTimeoutMs(Math.min(modelRequestTimeoutMs, ...timeoutBounds));
	return clampTimeoutMs(Math.min(isLocalRuntimeModel || isSelfHostedRuntimeModel ? LOCAL_LLM_FIRST_EVENT_TIMEOUT_MS : CLOUD_LLM_FIRST_EVENT_TIMEOUT_MS, ...timeoutBounds));
}

// ---- fixture cut ----

*/
function streamWithIdleTimeout(baseFn, timeoutMs, onIdleTimeout, opts) {
	const guardIterationGaps = opts?.scope !== "creation-only";
	const runId = opts?.runId;
	return (model, context, options) => {
		const trackCleanup = captureAsyncWorkTracker();
		const createIdleTimeoutError = () => /* @__PURE__ */ new Error(`LLM idle timeout (${Math.floor(timeoutMs / 1e3)}s): no response from model`);
		const streamAbortController = new AbortController();
		const sourceSignal = options?.signal;
		const abortStream = (reason) => {
			if (!streamAbortController.signal.aborted) streamAbortController.abort(reason);
		};
		const abortFromSourceSignal = () => abortStream(sourceSignal?.reason);
		if (sourceSignal?.aborted) abortFromSourceSignal();
		else sourceSignal?.addEventListener("abort", abortFromSourceSignal, { once: true });
		const cleanupSourceSignal = () => {
			sourceSignal?.removeEventListener("abort", abortFromSourceSignal);
		};
		const withSourceAbort = (promise) => sourceSignal ? abortable(sourceSignal, promise) : promise;
		const wrappedOptions = {
			...options,
			signal: streamAbortController.signal
		};
		const createTimeoutPromise = (setTimer) => {
			return new Promise((_, reject) => {
				const timer = setTimeout(() => {
					const error = createIdleTimeoutError();
					abortStream(error);
					onIdleTimeout?.(error);
					reject(error);
				}, timeoutMs);
				timer.unref?.();
				setTimer(timer);
			});
		};
		let maybeStream;
		try {
			maybeStream = baseFn(model, context, wrappedOptions);
		} catch (error) {
			cleanupSourceSignal();
			throw error;
		}
		const wrapStream = (stream) => {
			const originalAsyncIterator = stream[Symbol.asyncIterator].bind(stream);
			stream[Symbol.asyncIterator] = function() {
				const iterator = originalAsyncIterator();
				let returning;
				const returnIterator = (value) => {
					returning ??= trackCleanup(() => Promise.resolve().then(() => iterator.return?.(value) ?? {
						done: true,
						value: void 0
					}));
					returning.catch(() => recordAgentCleanupFailure());
					return returning;
				};
				const producerCompletion = getEventStreamCompletion(stream);
				let idleTimer = null;
				let rejectIdleTimeout;
				let firstArmPending = true;
				let streamFirstArmDone = false;
				let settled = false;
				const clearTimer = () => {
					if (idleTimer) {
						clearTimeout(idleTimer);
						idleTimer = null;
					}
				};
				const armTimer = () => {
					clearTimer();
					if (!guardIterationGaps || settled || !producerCompletion && !rejectIdleTimeout) return;
					const activeToolMs = runId ? getLastToolActivityMs(runId) : 0;
					const recentActivity = activeToolMs > 0 && Date.now() - activeToolMs < timeoutMs;
					const isFirstStreamArm = firstArmPending && !streamFirstArmDone;
					const effectiveTimeout = isFirstStreamArm && recentActivity ? Math.max(1, timeoutMs - Math.max(0, Date.now() - activeToolMs)) : timeoutMs;
					firstArmPending = false;
					if (isFirstStreamArm) streamFirstArmDone = true;
					idleTimer = setTimeout(() => {
						idleTimer = null;
						const error = createIdleTimeoutError();
						abortStream(error);
						onIdleTimeout?.(error);
						rejectIdleTimeout?.(error);
					}, effectiveTimeout);
					idleTimer.unref?.();
				};
				const unsubscribeLlmActivity = onLlmRequestActivity(streamAbortController.signal, () => {
					armTimer();
					if (runId && areDiagnosticsEnabledForProcess()) markDiagnosticRunProgress({
						runId,
						reason: "model_call:stream_progress"
					});
				});
				const unsubscribeStreamToolActivity = runId ? onToolActivity(runId, armTimer) : void 0;
				const settle = () => {
					if (settled) return;
					settled = true;
					rejectIdleTimeout = void 0;
					clearTimer();
					unsubscribeLlmActivity();
					unsubscribeStreamToolActivity?.();
					cleanupSourceSignal();
				};
				producerCompletion?.then(settle, settle);
				return createStreamIteratorWrapper({
					iterator,
					next: async (streamIterator) => {
						let pendingNext;
						try {
							const timeoutPromise = new Promise((_, reject) => {
								rejectIdleTimeout = reject;
								firstArmPending = true;
								armTimer();
							});
							pendingNext = streamIterator.next();
							const result = await withSourceAbort(Promise.race([pendingNext, timeoutPromise]));
							if (result.done) {
								settle();
								return result;
							}
							rejectIdleTimeout = void 0;
							armTimer();
							return result;
						} catch (error) {
							settle();
							trackCleanup(() => Promise.allSettled([pendingNext, returnIterator()])).catch(() => recordAgentCleanupFailure());
							throw error;
						}
					},
					onReturn(_streamIterator, value) {
						settle();
						return returnIterator(value);
					},
					onThrow(streamIterator, error) {
						settle();
						return streamIterator.throw?.(error) ?? Promise.reject(toErrorObject$1(error, "Non-Error rejection"));
					}
				});
			};
			return stream;
		};
		if (maybeStream && typeof maybeStream === "object" && "then" in maybeStream) {
			const source = Promise.resolve(maybeStream);
			let streamPromiseTimer = null;
			const clearStreamPromiseTimer = () => {
				if (streamPromiseTimer) {
					clearTimeout(streamPromiseTimer);
					streamPromiseTimer = null;
				}
			};
			const timeoutPromise = createTimeoutPromise((timer) => {
				streamPromiseTimer = timer;
			});
			return withSourceAbort(Promise.race([source, timeoutPromise])).then((stream) => {
				clearStreamPromiseTimer();
				return wrapStream(stream);
			}, (error) => {
				clearStreamPromiseTimer();
				cleanupSourceSignal();
				trackCleanup(async () => {
					const late = await source.catch(() => void 0);
					if (late) await late[Symbol.asyncIterator]().return?.();
				}).catch(() => recordAgentCleanupFailure());
				throw error;
			});
		}
		return wrapStream(maybeStream);
	};
}

// ---- fixture cut ----

	init_diagnostic_run_activity();
	init_async_work_scope();
	init_run_cleanup_timeout();
	init_stream_iterator_wrapper();
	init_abortable();
	init_tool_activity_heartbeat();
	DEFAULT_LLM_IDLE_TIMEOUT_MS = 12e4;
	SELF_HOSTED_LLM_IDLE_TIMEOUT_MS = 3e5;
	CLOUD_LLM_FIRST_EVENT_TIMEOUT_MS = DEFAULT_LLM_IDLE_TIMEOUT_MS;
	LOCAL_LLM_FIRST_EVENT_TIMEOUT_MS = 3e5;
	CRON_LLM_IDLE_TIMEOUT_MS = 6e4;
	LOCAL_PROVIDER_AUTH_MARKERS = /* @__PURE__ */ new Set(["custom-local", "ollama-local"]);
	SELF_HOSTED_PROVIDER_ID_PREFIXES = [

// ---- fixture cut ----

	if (firstEventTimeoutMs > 0) {
		const baseStreamFn = session.agent.streamFn;
		session.agent.streamFn = (model, context, options) => {
			const optionsWithFirstEvent = options;
			return baseStreamFn(model, context, {
				...options,
				firstEventTimeoutMs: optionsWithFirstEvent?.firstEventTimeoutMs ?? firstEventTimeoutMs,
				onFirstEventTimeout: optionsWithFirstEvent?.onFirstEventTimeout ?? callbacks.onIdleTimeout
			});
		};
	}
	let diagnosticModelCallSeq = 0;
	let modelResponseTerminal = false;

// ---- fixture cut ----

			payload: {
				kind: "agentTurn",
				message: value.message,
				model: value.model,
				thinking: value.thinking,
				timeoutSeconds: value.timeoutSeconds,
				allowUnsafeExternalContent: value.allowUnsafeExternalContent,
				externalContentSource: value.externalContentSource
			},
			delivery: value.delivery,
			state: { nextRunAtMs: nowMs }
		};
		let hookEventTarget;

// ---- fixture cut ----

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
