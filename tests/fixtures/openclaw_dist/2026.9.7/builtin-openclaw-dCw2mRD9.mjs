function shouldRepairMalformedToolCallArguments(params) {
	const modelApi = params.modelApi ?? "";
	return normalizeProviderId(params.provider ?? "") === "kimi" && modelApi === "anthropic-messages" || modelApi === "openai-completions" || TOOLCALL_REPAIR_RESPONSES_APIS.has(modelApi);
}
//#endregion
//#region src/agents/embedded-agent-runner/run/llm-idle-timeout.ts
const DEFAULT_LLM_IDLE_TIMEOUT_MS = 12e4;
const SELF_HOSTED_LLM_IDLE_TIMEOUT_MS = 3e5;
const CLOUD_LLM_FIRST_EVENT_TIMEOUT_MS = DEFAULT_LLM_IDLE_TIMEOUT_MS;
const LOCAL_LLM_FIRST_EVENT_TIMEOUT_MS = 3e5;
const CRON_LLM_IDLE_TIMEOUT_MS = 6e4;
const LOCAL_PROVIDER_AUTH_MARKERS = /* @__PURE__ */ new Set(["custom-local", "ollama-local"]);
const SELF_HOSTED_PROVIDER_ID_PREFIXES = [

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
						return streamIterator.throw?.(error) ?? Promise.reject(toErrorObject(error, "Non-Error rejection"));
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
