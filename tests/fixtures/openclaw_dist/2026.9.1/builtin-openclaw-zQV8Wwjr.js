/**
* Wraps LLM streams with idle-timeout detection and diagnostics.
*/
/**
* Default idle timeout for LLM streaming responses in milliseconds.
*/
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
	const clampTimeoutMs = (valueMs) => clampTimerTimeoutMs(valueMs) ?? 1;
	const runTimeoutMs = params?.runTimeoutMs;
	const agentTimeoutMs = finiteSecondsToTimerSafeMilliseconds(params?.cfg?.agents?.defaults?.timeoutSeconds);
	const hasExplicitRunTimeout = typeof runTimeoutMs === "number" && Number.isFinite(runTimeoutMs) && runTimeoutMs > 0;
	const runTimeoutIsBounded = hasExplicitRunTimeout && runTimeoutMs < 2147e6;
	const { isLocalRuntimeModel, isExplicitLocalHostnameRuntimeModel, isSelfHostedHostnameRuntimeModel } = resolveRuntimeModelLocality(params);
	const isSelfHostedRuntimeModel = isSelfHostedProviderId(params?.model?.provider) && !isCloudModelRef(params?.model?.id);
	const timeoutBounds = [runTimeoutIsBounded ? runTimeoutMs : void 0, hasExplicitRunTimeout ? void 0 : agentTimeoutMs].filter((value) => typeof value === "number" && Number.isFinite(value) && value > 0 && value < 2147e6);
	const modelRequestTimeoutMs = params?.modelRequestTimeoutMs;
	if (typeof modelRequestTimeoutMs === "number" && Number.isFinite(modelRequestTimeoutMs) && modelRequestTimeoutMs > 0) return clampTimeoutMs(Math.min(modelRequestTimeoutMs, ...timeoutBounds));
	return clampTimeoutMs(Math.min(isLocalRuntimeModel || isExplicitLocalHostnameRuntimeModel || isSelfHostedHostnameRuntimeModel || isSelfHostedRuntimeModel ? LOCAL_LLM_FIRST_EVENT_TIMEOUT_MS : CLOUD_LLM_FIRST_EVENT_TIMEOUT_MS, ...timeoutBounds));
}

// ---- fixture cut ----

*/
function streamWithIdleTimeout(baseFn, timeoutMs, onIdleTimeout, opts) {
	const guardIterationGaps = opts?.scope !== "creation-only";
	const runId = opts?.runId;
	return (model, context, options) => {
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
				let idleTimer = null;
				let waitingForProvider = false;
				let rejectIdleTimeout;
				let firstArmPending = true;
				let streamFirstArmDone = false;
				const clearTimer = () => {
					if (idleTimer) {
						clearTimeout(idleTimer);
						idleTimer = null;
					}
				};
				const armTimer = () => {
					clearTimer();
					if (!guardIterationGaps || !waitingForProvider) return;
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
				const stopWaiting = () => {
					waitingForProvider = false;
					rejectIdleTimeout = void 0;
					clearTimer();
				};
				const unsubscribeLlmActivity = onLlmRequestActivity(streamAbortController.signal, () => {
					armTimer();
					if (runId && areDiagnosticsEnabledForProcess()) markDiagnosticRunProgress({
						runId,
						reason: "model_call:stream_progress"
					});
				});
				const unsubscribeStreamToolActivity = runId ? onToolActivity(runId, armTimer) : void 0;
				const cleanupIterator = () => {
					stopWaiting();
					unsubscribeLlmActivity();
					unsubscribeStreamToolActivity?.();
					cleanupSourceSignal();
				};
				return createStreamIteratorWrapper({
					iterator,
					next: async (streamIterator) => {
						waitingForProvider = true;
						try {
							const timeoutPromise = new Promise((_, reject) => {
								rejectIdleTimeout = reject;
								firstArmPending = true;
								armTimer();
							});
							const result = await withSourceAbort(Promise.race([streamIterator.next(), timeoutPromise]));
							if (result.done) {
								cleanupIterator();
								return result;
							}
							stopWaiting();
							return result;
						} catch (error) {
							cleanupIterator();
							throw error;
						}
					},
					onReturn(streamIterator) {
						cleanupIterator();
						return streamIterator.return?.() ?? Promise.resolve({
							done: true,
							value: void 0
						});
					},
					onThrow(streamIterator, error) {
						cleanupIterator();
						return streamIterator.throw?.(error) ?? Promise.reject(toErrorObject(error, "Non-Error rejection"));
					}
				});
			};
			return stream;
		};
		if (maybeStream && typeof maybeStream === "object" && "then" in maybeStream) {
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
			return withSourceAbort(Promise.race([Promise.resolve(maybeStream), timeoutPromise])).then((stream) => {
				clearStreamPromiseTimer();
				return wrapStream(stream);
			}, (error) => {
				clearStreamPromiseTimer();
				cleanupSourceSignal();
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
				onFirstEventTimeout: optionsWithFirstEvent?.onFirstEventTimeout ?? input.onIdleTimeout
			});
		};
	}
	let diagnosticModelCallSeq = 0;
	session.agent.streamFn = wrapStreamFnWithDiagnosticModelCallEvents(session.agent.streamFn, {
