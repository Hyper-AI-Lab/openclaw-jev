const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const LOG = '/root/.openclaw/logs/rmp_adapter.log';
const RMP_API = 'http://127.0.0.1:8000';
const SETTINGS_PATH = '/root/.openclaw/rmp/settings.json';

/** The log is never a place for the RMP key or any auth header value. */
function redactSecrets(text) {
  let out = String(text);
  try {
    const key = JSON.parse(fs.readFileSync(SETTINGS_PATH, 'utf8')).api_key;
    if (key) out = out.split(key).join('***');
  } catch (_) {}
  return out.replace(/(X-RMP-API-Key:?\s*|Bearer\s+|x-access-token:)[^\s"'@]+/gi, '$1***');
}

function log(msg) {
  try {
    fs.appendFileSync(LOG, `[${new Date().toISOString()}] ${redactSecrets(msg)}\n`, { mode: 0o600 });
  } catch (_) {}
}

function loadSettings() {
  try {
    return JSON.parse(fs.readFileSync(SETTINGS_PATH, 'utf8'));
  } catch (_) {
    return {};
  }
}

function isDevSuspended() {
  const s = loadSettings();
  return !!(s.development_mode && s.suspend_task_interception);
}

function getApiKey() {
  return loadSettings().api_key || '';
}

function rmpHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  const key = getApiKey();
  if (key) headers['X-RMP-API-Key'] = key;
  return headers;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isLikelyTimeoutError(err) {
  // AbortSignal.timeout rejects fetch with a DOMException named TimeoutError.
  if (err && err.name === 'TimeoutError') return true;
  const msg = String(err && err.message ? err.message : err || '');
  return /timed?\s*out|ETIMEDOUT|timeout/i.test(msg);
}

/** RMP chat.postMessage without Aura. Fail closed if this also fails. */
async function notifyRmpUser(sessionKey, reason, content) {
  try {
    const idem = crypto.createHash('sha256')
      .update(`${sessionKey}:${reason}:${content || ''}`)
      .digest('hex')
      .slice(0, 32);
    const data = await rmpFetch('POST', '/api/notify-user', {
      session_key: sessionKey,
      reason,
      idempotency_key: idem,
    }, { maxTimeSec: 10 });
    if (!data || data.delivered !== true) {
      log(`RMP user notice not delivered (${reason}) on ${sessionKey}`);
      return false;
    }
    log(`RMP user notice sent (${reason}) on ${sessionKey}`);
    return true;
  } catch (e) {
    log(`RMP user notice failed (${reason}): ${e.message}`);
    return false;
  }
}

function intakePostTimeoutSec() {
  try {
    const settings = JSON.parse(fs.readFileSync('/root/.openclaw/rmp/settings.json', 'utf8'));
    const reg = settings.task_registry || {};
    const llm = Number(reg.intake_llm_timeout_sec) || 40;
    const ctx = Number(reg.intake_vector_deadline_sec) || 10;
    // workflow_execution budget is llm + context + 45; add slack for the rest of POST /tasks.
    return Math.ceil(llm + ctx + 45 + 30);
  } catch (_) {
    return 125;
  }
}

/**
 * RMP HTTP. Never block the event loop: the same gateway serves the intake
 * LLM leg (POST /hooks/agent) while POST /tasks is still waiting on it.
 */
async function rmpFetch(method, urlPath, body, opts) {
  const maxTimeSec = (opts && opts.maxTimeSec)
    || (method === 'POST' && urlPath === '/tasks' ? intakePostTimeoutSec() : 15);
  const init = {
    method,
    headers: rmpHeaders(),
    signal: AbortSignal.timeout(maxTimeSec * 1000),
  };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
  }
  const res = await fetch(`${RMP_API}${urlPath}`, init);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : {}; } catch (_) { data = { raw: text }; }
  if (res.status >= 400) {
    const detail = typeof data.detail === 'string' ? data.detail : data.detail ? JSON.stringify(data.detail) : data.raw;
    throw new Error(`HTTP ${res.status}${detail ? `: ${String(detail).slice(0, 500)}` : ''}`);
  }
  return data;
}

function extractText(msg) {
  if (!msg?.content || !Array.isArray(msg.content)) return '';
  let text = '';
  for (const part of msg.content) {
    if (part?.type === 'text' && part.text) text += part.text;
  }
  return text;
}

/** The Slack message as RMP needs it: text, identity, what it replies to, attachments. */
function inboundFromEvent(event) {
  const media = Array.isArray(event?.media) ? event.media : [];
  const attachments = media
    .map((m) => {
      const where = String(m?.path || m?.url || '');
      return {
        path: where,
        type: String(m?.contentType || m?.kind || ''),
        name: String(m?.fileName || m?.name || path.basename(where) || 'attachment'),
      };
    })
    .filter((a) => a.path);
  const replyToId = String(event?.replyToId || event?.threadId || '');
  return {
    content: String(event?.content || event?.body || '').trim(),
    messageId: String(event?.messageId || '') || null,
    senderId: String(event?.senderId || '') || null,
    timestamp: Number.isFinite(event?.timestamp) ? event.timestamp : null,
    threadId: String(event?.threadId || '') || null,
    replyTo: replyToId
      ? { id: replyToId, body: String(event?.replyToBody || '').slice(0, 2000), sender: String(event?.replyToSender || '') }
      : null,
    attachments,
  };
}

/** Message text with the attachments Aura can open. */
function textWithAttachments(inbound) {
  if (!inbound.attachments.length) return inbound.content;
  const lines = inbound.attachments.map((a) => `- ${a.name}${a.type ? ` (${a.type})` : ''}: ${a.path}`);
  return `${inbound.content || 'Kirill sent an attachment.'}\n\n[Attachments]\n${lines.join('\n')}`;
}

function describeError(e) {
  if (e instanceof Error) return `${e.message}${e.stack ? ` | ${e.stack.split('\n').slice(1, 3).join(' ').trim()}` : ''}`;
  if (e && typeof e === 'object') {
    try { return JSON.stringify(e).slice(0, 500); } catch (_) { return Object.prototype.toString.call(e); }
  }
  return String(e);
}

function isStopCommand(intent) {
  const t = String(intent || '').trim();
  return /^(?:[.!?,:;]+\s*)?(?:please\s+)?(stop|abort|cancel|halt)(?:[.!?]*)?$/i.test(t);
}

function isSlackConversationKey(sessionKey) {
  return String(sessionKey || '').includes('slack:');
}

const SESSIONS_JSON = '/root/.openclaw/agents/main/sessions/sessions.json';
const AGENT_SQLITE = '/root/.openclaw/agents/main/agent/openclaw-agent.sqlite';
let _slackKeyCache = { key: '', ts: 0 };

function findSlackSessionKeyFromStore() {
  const now = Date.now();
  if (_slackKeyCache.key && now - _slackKeyCache.ts < 60000) {
    return _slackKeyCache.key;
  }
  let found = '';
  try {
    const store = JSON.parse(fs.readFileSync(SESSIONS_JSON, 'utf8'));
    const keys = Object.keys(store).filter((k) => k.includes('slack:channel:'));
    keys.sort((a, b) => {
      const da = a.toLowerCase().includes('slack:channel:d') ? 0 : 1;
      const db = b.toLowerCase().includes('slack:channel:d') ? 0 : 1;
      return da - db;
    });
    found = keys[0] || '';
  } catch (_) {}
  if (!found) {
    try {
      const { DatabaseSync } = require('node:sqlite');
      const db = new DatabaseSync(AGENT_SQLITE, { readOnly: true });
      try {
        const keys = db.prepare("select session_key from session_nodes where session_key like '%slack:channel:%'")
          .all()
          .map((row) => String(row.session_key));
        const sortKey = (k) => `${k.toLowerCase().includes('slack:channel:d') ? 0 : 1}${k.toLowerCase()}`;
        keys.sort((a, b) => (sortKey(a) < sortKey(b) ? -1 : sortKey(a) > sortKey(b) ? 1 : 0));
        found = keys[0] || '';
      } finally {
        db.close();
      }
    } catch (_) {
      found = '';
    }
  }
  if (found) _slackKeyCache = { key: found, ts: now };
  return found;
}

function pickSlackSessionKey(event, ctx) {
  const candidates = [
    event && event.sessionKey,
    ctx && ctx.sessionKey,
    event && event.metadata && event.metadata.sessionKey,
    ctx && ctx.origin && ctx.origin.sessionKey,
  ];
  const slack = candidates.filter((k) => isSlackConversationKey(k));
  slack.sort((a, b) => {
    const da = String(a).toLowerCase().includes('slack:channel:d') ? 0 : 1;
    const db = String(b).toLowerCase().includes('slack:channel:d') ? 0 : 1;
    return da - db;
  });
  if (slack[0]) return String(slack[0]);
  const fromStore = findSlackSessionKeyFromStore();
  if (fromStore) return fromStore;
  for (const k of candidates) {
    if (k) return String(k);
  }
  return 'agent:main:main';
}

function isHeartbeatMessage(text) {
  return text.includes('HEARTBEAT.md') ||
    (text.startsWith('[cron:') && /heartbeat/i.test(text));
}

function isCronMessage(text) {
  return text.startsWith('[cron:') || text.includes('[cron:');
}

function stripSystemAcks(text) {
  let t = (text || '').replace(/\[\[reply_to_current\]\]/gi, ' ').trim();
  while (/^(HEARTBEAT_OK|CANARY_OK)\b/i.test(t)) {
    t = t.replace(/^(HEARTBEAT_OK|CANARY_OK)\s*/i, '').trim();
  }
  return t.replace(/\s+/g, ' ').trim();
}

function isPureSystemAck(text) {
  const stripped = stripSystemAcks(text);
  return !stripped;
}

function sanitizeSlackOutbound(text) {
  let t = (text || '').trim();
  t = t.replace(/\{[^{}]*"(?:facts|task_status)"[^{}]*\}/gi, '');
  t = t.replace(/```json\s*\{[\s\S]*?\}\s*```/gi, '');
  t = t.replace(/^Origin:\s.*$/gm, '');
  t = t.replace(/^Session:\s.*$/gm, '');
  t = t.replace(/^Timestamp:\s.*$/gm, '');
  t = t.replace(/^Model:\s.*$/gm, '');
  t = t.replace(/^RMP integration:\s.*$/gm, '');
  t = t.replace(/^Memory status:\s.*$/gm, '');
  t = t.replace(/^Goal:\s.*$/gm, '');
  t = t.replace(/^Emotional state:\s.*$/gm, '');
  t = t.replace(/^EOF\s*$/gm, '');
  t = t.replace(/\[INTERNAL_RMP\]/g, '');
  if (t.length >= 40) {
    const mid = Math.floor(t.length / 2);
    for (const pivot of [mid, mid + 1]) {
      const first = t.slice(0, pivot).trim();
      const second = t.slice(pivot).trim();
      if (first && first === second) {
        t = first;
        break;
      }
    }
  }
  return t.replace(/\n{3,}/g, '\n\n').trim();
}

function clearMainSessionSendPolicy() {
  const sessionsPath = '/root/.openclaw/agents/main/sessions/sessions.json';
  try {
    const store = JSON.parse(fs.readFileSync(sessionsPath, 'utf8'));
    let changed = false;
    for (const [key, entry] of Object.entries(store)) {
      if (!entry || entry.sendPolicy !== 'deny') continue;
      if (key === 'agent:main:main' || key.includes('slack:') || key.endsWith(':main')) {
        delete entry.sendPolicy;
        changed = true;
        log(`Cleared sendPolicy=deny on ${key} (was blocking inbound Slack)`);
      }
    }
    if (changed) {
      fs.writeFileSync(sessionsPath, JSON.stringify(store, null, 2));
    }
  } catch (e) {
    log(`sendPolicy clear skipped: ${e.message}`);
  }
}

/** One key per Slack message: its id when Slack gave one, else its text. */
function inboundIdempotencyKey(sessionKey, rawText, intent, heartbeatKey, messageId) {
  if (heartbeatKey) {
    return crypto.createHash('sha256').update(`${sessionKey}:${heartbeatKey}`).digest('hex');
  }
  if (messageId) {
    return crypto.createHash('sha256').update(`${sessionKey}:msg:${messageId}`).digest('hex');
  }
  return crypto.createHash('sha256').update(`${sessionKey}:${rawText || intent}`).digest('hex');
}

async function createRmpTaskFromInbound({ sessionKey, intent, tags, rawText, heartbeatKey, inbound }) {
  const idemKey = inboundIdempotencyKey(sessionKey, rawText, intent, heartbeatKey, inbound?.messageId);
  const data = await rmpFetch('POST', '/tasks', {
    intent: (rawText || intent || "").slice(0, 20000),
    tags: tags || ['user-request'],
    user_id: 'slack_user',
    session_key: sessionKey,
    raw_text: rawText || intent,
    // No process_type_hint: intake LLM + memory/registry decide routing.
    idempotency_key: idemKey,
    slack_message_id: inbound?.messageId || null,
    slack_user_id: inbound?.senderId || null,
    slack_event_ts: inbound?.timestamp ?? null,
    thread_id: inbound?.threadId || null,
    reply_to: inbound?.replyTo || null,
    attachments: inbound?.attachments?.length ? inbound.attachments : null,
  });
  if (data.skipped) {
    log(`INTAKE skipped: ${data.intake_action || 'skip'} — ${data.reason || ''}`);
    return data;
  }
  if (data.intake_action === 'wait_active') {
    log(`INTAKE wait_active on task ${data.task_id}`);
    return data;
  }
  if (data.intake_action === 'clarify') {
    log(`INTAKE clarify on task ${data.task_id} (no Aura yet)`);
    return data;
  }
  if (data.intake_action === 'resume_clarify') {
    log(`INTAKE resume_clarify on task ${data.task_id} workflow=${!!data.workflow_started}`);
    return data;
  }
  if (data.intake_action === 'attach_active') {
    log(`INTAKE attach_active on task ${data.task_id}`);
    return data;
  }
  if (data.intake_action === 'spawn_process') {
    log(`INTAKE spawn_process on task ${data.task_id} proc=${data.process_run_id} workflow=${!!data.workflow_started}`);
    return data;
  }
  if (data.task_id) {
    await prefetchProcessMemory(data.task_id, data.process_run_id);
  }
  const terminal = new Set(['failed', 'completed', 'stopped_by_user', 'cancelled']);
  if (data.deduplicated && terminal.has(data.status)) {
    log(`Prior task ${data.task_id} is ${data.status}; retrying`);
    const retried = await rmpFetch('POST', `/tasks/${data.task_id}/retry`);
    log(`Retried as task ${retried.task_id}`);
  } else {
    log(`Created task ${data.task_id} (dedup=${!!data.deduplicated})`);
  }
  return data;
}

async function routeSlackDmToRmp(inbound, sessionKey) {
  if (isDevSuspended()) {
    log('DEV MODE: Slack DM absorbed (no task, no delivery)');
    return true;
  }
  const intent = textWithAttachments(inbound).trim();
  if (!intent) return true;

  if (!inbound.attachments.length && isStopCommand(intent)) {
    try {
      const activeData = await rmpFetch('GET', `/sessions/${encodeURIComponent(sessionKey)}/active_user_task`);
      if (activeData.active_task?.id) {
        await rmpFetch('POST', `/tasks/${activeData.active_task.id}/signal`, {
          signal_type: 'user_input',
          message: intent,
        });
        log(`SIGNALED stop to task ${activeData.active_task.id}`);
      } else if (await notifyRmpUser(sessionKey, 'stop_idle', intent)) {
        log(`Stop with no active task; RMP ack on ${sessionKey}`);
      } else {
        log(`Stop with no active task; RMP notice was not delivered on ${sessionKey}`);
      }
    } catch (e) {
      log(`Signal error: ${describeError(e)}`);
      await notifyRmpUser(sessionKey, 'intake_unavailable', intent);
      throw e;
    }
    return true;
  }

  const payload = {
    sessionKey,
    intent,
    tags: ['user-request'],
    rawText: intent,
    inbound,
  };
  try {
    await createRmpTaskFromInbound(payload);
    return true;
  } catch (e) {
    // The fetch deadline can fire while POST /tasks is still running intake LLM.
    // Server may still create the task; recover via idempotent re-POST. Never
    // hand the turn back to native OpenClaw.
    if (isLikelyTimeoutError(e)) {
      log(`Intake POST timed out; recovering via idempotent re-POST: ${describeError(e)}`);
      for (const waitMs of [2000, 3000, 5000]) {
        await sleep(waitMs);
        try {
          const data = await createRmpTaskFromInbound(payload);
          log(`Recovered RMP ownership after timeout (task=${data?.task_id || '?'})`);
          return true;
        } catch (e2) {
          log(`Recovery attempt failed: ${describeError(e2)}`);
        }
        try {
          const idemKey = inboundIdempotencyKey(sessionKey, intent, intent, null, inbound.messageId);
          const found = await rmpFetch('GET', `/tasks/by-idempotency/${encodeURIComponent(idemKey)}`);
          if (found && found.task_id) {
            log(`Recovered RMP ownership via idempotency ${found.task_id}`);
            return true;
          }
        } catch (_) {}
      }
    }
    await notifyRmpUser(sessionKey, 'intake_unavailable', intent);
    throw e;
  }
}

/** One route at a time per session, in arrival order, so a stop sees the task it stops. */
const routeChains = new Map();
function routeInBackground(hookName, inbound, sessionKey) {
  const next = (routeChains.get(sessionKey) || Promise.resolve())
    .then(() => routeSlackDmToRmp(inbound, sessionKey))
    .catch((e) => log(`${hookName} route error (claimed; no native): ${describeError(e)}`));
  routeChains.set(sessionKey, next);
  next.then(() => {
    if (routeChains.get(sessionKey) === next) routeChains.delete(sessionKey);
  });
}

/** Recent Slack DMs claimed by one hook — avoid a second POST from the same event.
 * Keyed by Slack message id, so two quick identical texts are still two messages. */
const claimedSlackKeys = new Map();
function claimFingerprint(inbound) {
  if (inbound.messageId) return `id:${inbound.messageId}`;
  return crypto.createHash('sha256').update(textWithAttachments(inbound)).digest('hex');
}
function markSlackClaimed(sessionKey, inbound) {
  const fp = claimFingerprint(inbound);
  const now = Date.now();
  const aliases = new Set([sessionKey, 'agent:main:main']);
  const discovered = findSlackSessionKeyFromStore();
  if (discovered) aliases.add(discovered);
  for (const alias of aliases) {
    claimedSlackKeys.set(`${alias}::${fp}`, now);
  }
  if (claimedSlackKeys.size > 400) {
    const cutoff = Date.now() - 30 * 1000;
    for (const [k, ts] of claimedSlackKeys) {
      if (ts < cutoff) claimedSlackKeys.delete(k);
    }
  }
  return `${sessionKey}::${fp}`;
}
function wasSlackClaimed(sessionKey, inbound) {
  const fp = claimFingerprint(inbound);
  const aliases = new Set([sessionKey, 'agent:main:main']);
  const discovered = findSlackSessionKeyFromStore();
  if (discovered) aliases.add(discovered);
  const cutoff = Date.now() - 30 * 1000;
  for (const alias of aliases) {
    const ts = claimedSlackKeys.get(`${alias}::${fp}`);
    if (ts && ts >= cutoff) return true;
  }
  return false;
}

function isMainChatSession(sessionKey) {
  return sessionKey === 'agent:main:main' || (sessionKey || '').endsWith(':main');
}

function isRmpOwnedSlackSession(sessionKey) {
  const key = sessionKey || '';
  return isMainChatSession(key) || key.includes('slack:');
}

/** Last known active user task per session, for hooks that must answer synchronously. */
const activeUserTasks = new Map();

async function getActiveRmpUserTask(sessionKey) {
  if (!isRmpOwnedSlackSession(sessionKey)) return null;
  try {
    // Callers run inside message_sending / before_agent_run, which allow 15 s per handler.
    const data = await rmpFetch(
      'GET',
      `/sessions/${encodeURIComponent(sessionKey)}/active_user_task`,
      undefined,
      { maxTimeSec: 5 }
    );
    const task = data.active_task || null;
    activeUserTasks.set(sessionKey, task);
    return task;
  } catch (_) {
    return null;
  }
}

async function prefetchProcessMemory(taskId, processRunId) {
  if (!processRunId) return null;
  try {
    const ctx = await rmpFetch('GET', `/memory/process/${encodeURIComponent(processRunId)}/context`);
    const path = `/tmp/rmp_ctx_${taskId}.json`;
    fs.writeFileSync(path, JSON.stringify({ task_id: taskId, process_run_id: processRunId, ...ctx }));
    log(`Prefetched memory context for ${taskId} -> ${path}`);
    return ctx;
  } catch (e) {
    log(`Memory prefetch skipped: ${e.message}`);
    return null;
  }
}

function looksLikeInterimAgentText(text) {
  const t = (text || '').trim();
  if (!t) return true;
  const lower = t.toLowerCase();
  if (/^let me (check|look|see|read|also)/i.test(t)) return true;
  if (/\[tool call:/i.test(t)) return true;
  if (!/[\u0400-\u04FF]/.test(t) && !/[\u0600-\u06FF]/.test(t)) {
    return false;
  }
  // Mixed-script planning monologue while tools are running — not a user reply.
  return lower.includes('let me') || lower.includes('need to read') || lower.includes('check the');
}

function pinSessionProfile(sessionKey, profileId) {
  const sessionsPath = '/root/.openclaw/agents/main/sessions/sessions.json';
  try {
    const store = JSON.parse(fs.readFileSync(sessionsPath, 'utf8'));
    const entry = store[sessionKey] || (store[sessionKey] = {});
    entry.authProfileOverride = profileId;
    entry.authProfileOverrideSource = 'user';
    fs.writeFileSync(sessionsPath, JSON.stringify(store, null, 2));
  } catch (e) {
    log(`Session profile pin failed: ${e.message}`);
  }
}

async function reserveLlmSlot(sessionKey) {
  const data = await rmpFetch('POST', '/api/llm/reserve', { session_key: sessionKey });
  if (data.pin_session && data.profile_id && sessionKey) {
    pinSessionProfile(sessionKey, data.profile_id);
  }
  return data;
}

async function releaseLlmSlot(sessionKey) {
  try {
    await rmpFetch('POST', '/api/llm/release', { session_key: sessionKey });
  } catch (e) {
    log(`Release slot failed for ${sessionKey}: ${e.message}`);
  }
}

function installNativeSlackSuppressor() {
  // OpenClaw Slack DM delivery (deliverReplies) bypasses message_sending hooks.
  // Patched dist calls this before chat.postMessage so RMP stays the sole reply path.
  // Hard policy: never allow native Slack replies (no intake-failure escape hatch).
  globalThis.__RMP_SUPPRESS_NATIVE_SLACK = (params) => {
    if (isDevSuspended()) return false;
    const target = String(params?.target || '');
    log(`SUPPRESSED native Slack deliverReplies (RMP owns delivery) target=${target.slice(0, 40)}`);
    return true;
  };
}

function isSlackInboundSession(sessionKey) {
  const key = sessionKey || '';
  return isMainChatSession(key) || key.includes('slack:');
}

/** Cron → RMP task, after before_message_write has already blocked the write. */
async function routeScheduledToRmp(payload) {
  const { sessionKey } = payload;
  try {
    const activeData = await rmpFetch(
      'GET',
      `/sessions/${encodeURIComponent(sessionKey)}/active_task`
    );
    if (activeData.active_task?.id) {
      log(`SKIP heartbeat/cron — active task ${activeData.active_task.id} on ${sessionKey}`);
      return;
    }
  } catch (e) {
    log(`Heartbeat active check error: ${e.message}`);
  }
  try {
    await createRmpTaskFromInbound(payload);
  } catch (e) {
    log(`Task creation FAILED (fail-closed): ${e.message}`);
  }
}

module.exports = {
  name: 'rmp_adapter',
  rmpFetch,
  register: (api) => {
    log('rmp_adapter plugin register() called');
    installNativeSlackSuppressor();
    clearMainSessionSendPolicy();

    api.registerTool({
      name: 'rmp_memory_recall',
      description: 'Recall process-scoped memory from RMP for the current or given task.',
      parameters: {
        type: 'object',
        properties: {
          process_run_id: { type: 'string' },
          query: { type: 'string' },
        },
        required: ['process_run_id'],
      },
      execute: async (_id, params) => {
        try {
          const q = params.query ? `?query=${encodeURIComponent(params.query)}` : '';
          const result = await rmpFetch(
            'GET',
            `/memory/process/${encodeURIComponent(params.process_run_id)}/context${q}`
          );
          const block = result.context_block || '(empty)';
          return `PROCESS-SCOPED MEMORY (${result.count || 0} items):\n${block}`;
        } catch (err) {
          return `Recall failed: ${err.message}`;
        }
      },
    });

    api.registerTool((context) => ({
      name: 'rmp_task_create',
      description: 'Create a durable task in the Reliability and Memory Plane.',
      parameters: {
        type: 'object',
        properties: {
          intent: { type: 'string' },
          task_type: { type: 'string' },
          tags: { type: 'array', items: { type: 'string' } }
        },
        required: ['intent', 'task_type']
      },
      execute: async (_id, params) => {
        try {
          const result = await rmpFetch('POST', '/tasks', {
            intent: params.intent,
            tags: params.tags || [],
            user_id: context?.session?.origin?.from || 'unknown',
            session_key: pickSlackSessionKey({}, context) || context?.sessionKey || 'agent:main:main',
            raw_text: params.intent
          });
          return `Task created: ${result.task_id}`;
        } catch (err) {
          return `Failed: ${err.message}`;
        }
      }
    }), { name: 'rmp_task_create' });

    api.registerTool({
      name: 'rmp_task_status',
      description: 'Check the status of a task in the RMP.',
      parameters: {
        type: 'object',
        properties: { task_id: { type: 'string' } },
        required: ['task_id']
      },
      execute: async (_id, params) => {
        try {
          const result = await rmpFetch('GET', `/tasks/${params.task_id}`);
          return `Task ${result.task_id}: ${result.status}`;
        } catch (err) {
          return `Failed: ${err.message}`;
        }
      }
    });

    require('./claude_tools').register(api, { rmpFetch });

    // A task session's exec output never holds more than 12,000 characters (see exec_result_cap.js).
    // OpenClaw versions without tool result middleware simply skip it.
    try {
      require('./exec_result_cap').register(api, { log });
    } catch (e) {
      log(`exec result cap not registered: ${e && e.message}`);
    }

    // Claim Slack DMs before OpenClaw's native agent turn. Returning
    // { handled: true } stops the gateway from answering (and double-posting).
    // Routing runs in the background: the claim must not wait on intake.
    // A message with neither text nor attachments carries nothing to route.
    const hasSubstance = (inbound) => Boolean(inbound.content || inbound.attachments.length);
    const preview = (inbound) =>
      `${inbound.content.slice(0, 80)}${inbound.attachments.length ? ` [+${inbound.attachments.length} attachment(s)]` : ''}`;

    api.on('inbound_claim', (event, ctx) => {
      try {
        if (isDevSuspended()) return;
        const channel = String(event?.channel || ctx?.channelId || '').toLowerCase();
        const inbound = inboundFromEvent(event);
        if (!hasSubstance(inbound)) return;
        if (channel !== 'slack' && !channel.includes('slack')) return;
        const sessionKey = pickSlackSessionKey(event, ctx);
        log(`inbound_claim slack DM on ${sessionKey}: ${preview(inbound)}`);
        markSlackClaimed(sessionKey, inbound);
        routeInBackground('inbound_claim', inbound, sessionKey);
        return { handled: true };
      } catch (e) {
        // Fail closed: still claim the turn so native OpenClaw cannot answer.
        log(`inbound_claim route error (still claiming; no native): ${describeError(e)}`);
        try {
          const inbound = inboundFromEvent(event);
          if (hasSubstance(inbound)) markSlackClaimed(pickSlackSessionKey(event, ctx), inbound);
        } catch (_) {}
        return { handled: true };
      }
    }, { priority: 120 });

    api.on('before_dispatch', (event, ctx) => {
      try {
        if (isDevSuspended()) return;
        const channel = String(event?.channel || ctx?.channelId || '').toLowerCase();
        const inbound = inboundFromEvent(event);
        if (!hasSubstance(inbound)) return;
        if (channel !== 'slack' && !channel.includes('slack')) return;
        const sessionKey = pickSlackSessionKey(event, ctx);
        if (wasSlackClaimed(sessionKey, inbound)) {
          log(`before_dispatch: already claimed on ${sessionKey}`);
          return { handled: true };
        }
        // Safety net if inbound_claim did not run for this build/path.
        log(`before_dispatch claiming slack DM on ${sessionKey}: ${preview(inbound)}`);
        markSlackClaimed(sessionKey, inbound);
        routeInBackground('before_dispatch', inbound, sessionKey);
        return { handled: true };
      } catch (e) {
        log(`before_dispatch route error (still claiming; no native): ${describeError(e)}`);
        try {
          const inbound = inboundFromEvent(event);
          if (hasSubstance(inbound)) markSlackClaimed(pickSlackSessionKey(event, ctx), inbound);
        } catch (_) {}
        return { handled: true };
      }
    }, { priority: 120 });

    // Backup if claim hooks are unavailable; skip when already claimed.
    api.on('message_received', (event, ctx) => {
      try {
        const meta = event?.metadata || {};
        const provider = String(meta.provider || meta.surface || meta.originatingChannel || ctx?.channelId || '').toLowerCase();
        const inbound = inboundFromEvent(event);
        if (!hasSubstance(inbound)) return;
        const isSlack = provider === 'slack' || provider.includes('slack');
        if (!isSlack) return;
        const sessionKey = pickSlackSessionKey(event, ctx);
        if (wasSlackClaimed(sessionKey, inbound)) {
          log(`message_received skip (already claimed) on ${sessionKey}`);
          return { handled: true };
        }
        log(`message_received slack DM on ${sessionKey}: ${preview(inbound)}`);
        markSlackClaimed(sessionKey, inbound);
        routeInBackground('message_received', inbound, sessionKey);
        return { handled: true };
      } catch (e) {
        log(`message_received route error (still claiming; no native): ${describeError(e)}`);
        try {
          const inbound = inboundFromEvent(event);
          if (hasSubstance(inbound)) markSlackClaimed(pickSlackSessionKey(event, ctx), inbound);
        } catch (_) {}
        return { handled: true };
      }
    }, { priority: 120 });

    api.on('reply_payload_sending', (event, ctx) => {
      try {
        if (isDevSuspended()) return;
        const sessionKey = event?.sessionKey || ctx?.sessionKey || '';
        const channel = String(event?.channel || ctx?.channelId || '').toLowerCase();
        const isSlack = channel === 'slack' || channel.includes('slack') || isSlackInboundSession(sessionKey);
        if (!isSlack) return;
        log(`reply_payload_sending cancel on ${sessionKey || channel} (RMP owns delivery)`);
        return { cancel: true, reason: 'rmp_owns_slack_delivery' };
      } catch (e) {
        log(`reply_payload_sending error: ${e.message}`);
      }
    }, { priority: 120 });

    api.on('before_message_write', (event, ctx) => {
      try {
        const msg = event?.message;
        if (!msg) return;

        const text = extractText(msg);
        if (!text) return;

        if (text.includes('[INTERNAL_RMP]') && !text.includes('[RMP_DELIVER]')) {
          log('BLOCKED internal RMP message');
          return { block: true };
        }
        if (text.includes('[RMP_DELIVER]')) {
          return { block: true };
        }
        if (text.includes('[SYSTEM ENFORCEMENT]') || text.includes('[SYSTEM NOTIFICATION]')) {
          return;
        }
        if (text.includes('Hook Hook:') || text.includes('Hook Hook (error)')) {
          return { block: true };
        }

        if (msg.role === 'assistant' && isSlackInboundSession(ctx?.sessionKey || '')) {
          if (isPureSystemAck(text)) {
            log('BLOCKED system ack from Slack session transcript');
            return { block: true };
          }
          if (!isDevSuspended()) {
            log(`BLOCKED Slack-session assistant msg (RMP owns delivery)`);
            return { block: true };
          }
          // OpenClaw ignores a Promise from this hook: decide from the last known task.
          const devSessionKey = ctx?.sessionKey || 'agent:main:main';
          const active = activeUserTasks.get(devSessionKey);
          void getActiveRmpUserTask(devSessionKey);
          if (active) {
            log(`BLOCKED main-session assistant msg during RMP task ${active.id}`);
            return { block: true };
          }
        }

        if (msg.role !== 'user') return;
        if (text.startsWith('System:') && !text.includes('Slack DM from')) return;
        if (text.includes('OpenClaw runtime context (internal)')) return;
        if (text.includes('Subagent Context') && text.includes('auto-announce')) return;

        const isSlackDM = text.includes('Slack DM from');
        const isCron = isCronMessage(text);
        const isHeartbeat = isHeartbeatMessage(text);

        if (!isSlackDM && !isCron && !isHeartbeat) return;

        if (isDevSuspended()) {
          log('DEV MODE: message absorbed (no task, no delivery)');
          return { block: true };
        }

        // Heartbeat is off by policy (every "0m"). If one runs anyway, it must not spawn RMP workflows.
        if (isHeartbeat && !isSlackDM) {
          log('SKIP RMP routing for internal heartbeat');
          return { block: true };
        }

        if (isSlackDM) {
          log('BLOCKED Slack DM write on main session (RMP owns intake)');
          return { block: true };
        }

        const sessionKey = ctx?.sessionKey || 'agent:main:main';

        let intent = text;
        if (isCron) {
          intent = text.replace(/^\[cron:[^\]]*\]\s*/, '').trim();
        }

        if (isCron || isHeartbeat) {
          let idemKey;
          if (isHeartbeat || (isCron && sessionKey.includes('heartbeat'))) {
            idemKey = 'heartbeat-v1';
          } else {
            idemKey = null;
          }
          void routeScheduledToRmp({
            sessionKey,
            intent: (intent || "").slice(0, 20000),
            tags: isCron ? ['cron'] : isHeartbeat ? ['heartbeat'] : ['user-request'],
            rawText: intent,
            heartbeatKey: idemKey,
          });
          return { block: true };
        }
      } catch (e) {
        log(`Hook error: ${e.message}`);
        return { block: true };
      }
    }, { priority: 100 });

    api.on('message_sending', async (event, ctx) => {
      const content = event?.content || '';
      if (!content.trim()) return { cancel: true };

      const sessionKey = ctx?.sessionKey || event?.sessionKey || 'agent:main:main';

      // RMP owns all Slack DM delivery; native gateway must never post.
      if (isSlackInboundSession(sessionKey) && !isDevSuspended()) {
        log(`SUPPRESSED native Slack on ${sessionKey} (RMP owns delivery)`);
        return { cancel: true };
      }

      const active = await getActiveRmpUserTask(sessionKey);
      if (active) {
        log(`SUPPRESSED native Slack delivery during RMP task ${active.id}`);
        return { cancel: true };
      }
      if (looksLikeInterimAgentText(content)) {
        log('SUPPRESSED interim tool-planning text from Slack delivery');
        return { cancel: true };
      }

      if (isPureSystemAck(content)) {
        log('SUPPRESSED pure system ack from Slack delivery');
        return { cancel: true };
      }
      const stripped = stripSystemAcks(sanitizeSlackOutbound(content));
      if (!stripped) return { cancel: true };
      if (stripped !== content.trim()) {
        log('SANITIZED outbound Slack text (facts/metadata/interim stripped)');
        return { content: stripped };
      }
    }, { priority: 100 });

    // A tool that waits for Kirill's answer cannot reach him from an RMP run (RMP owns every
    // Slack message), so it would hold the run until its timeout.
    api.on('before_tool_call', (event, ctx) => {
      const sessionKey = ctx?.sessionKey || '';
      if (!sessionKey.includes('rmp_task_') && !sessionKey.includes('rmp_verify_') && !sessionKey.includes('rmp_intake_')) {
        return;
      }
      const tool = event?.toolName;
      if (tool === 'ask_user' || (tool === 'secrets' && event?.params?.action === 'request')) {
        log(`Blocked ${tool} in ${sessionKey}: Kirill cannot answer tool prompts in an RMP run`);
        return {
          block: true,
          blockReason:
            'Kirill cannot answer tool prompts during an RMP task. Put the question in your reply; RMP delivers it and brings his answer back as a follow-up.',
        };
      }
    }, { priority: 100 });

    // Balanced NVIDIA key rotation + concurrency cap for gateway agent runs.
    // RMP-owned sessions (rmp_task_*/rmp_verify_*/rmp_intake_*) reserve/release inside the worker.
    api.on('before_agent_run', async (event, ctx) => {
      const sessionKey = ctx?.sessionKey || '';
      const trigger = ctx?.trigger || '';
      if (!sessionKey || sessionKey.includes('rmp_task_') || sessionKey.includes('rmp_verify_') || sessionKey.includes('rmp_intake_')) {
        return { outcome: 'pass' };
      }
      if (trigger === 'heartbeat') {
        log(`SKIP LLM reserve for heartbeat on ${sessionKey}`);
        return { outcome: 'pass' };
      }
      if (isRmpOwnedSlackSession(sessionKey) && !isDevSuspended()) {
        log(`SKIP LLM reserve on Slack/main session (RMP owns Slack path): ${sessionKey}`);
        return { outcome: 'pass' };
      }
      // Dev-mode native runs: refresh the task before_message_write checks without network.
      if (isRmpOwnedSlackSession(sessionKey)) {
        await getActiveRmpUserTask(sessionKey);
      }
      try {
        const data = await rmpFetch('POST', '/api/llm/reserve', { session_key: sessionKey });
        if (data.pin_session && data.profile_id) {
          pinSessionProfile(sessionKey, data.profile_id);
        }
        log(`Reserved ${data.profile_id} for ${sessionKey} (${data.orchestration?.active_slots || '?'}/${data.orchestration?.max_concurrent || '?'} slots)`);
      } catch (e) {
        log(`LLM reserve failed for ${sessionKey}: ${e.message}`);
      }
      return { outcome: 'pass' };
    }, { priority: 110 });

    api.on('agent_end', async (event, ctx) => {
      const sessionKey = ctx?.sessionKey || '';
      if (!sessionKey || sessionKey.includes('rmp_task_') || sessionKey.includes('rmp_verify_') || sessionKey.includes('rmp_intake_')) {
        return;
      }
      try {
        await rmpFetch('POST', '/api/llm/release', { session_key: sessionKey });
        log(`Released LLM slot for ${sessionKey}`);
      } catch (e) {
        log(`LLM release failed for ${sessionKey}: ${e.message}`);
      }
    }, { priority: 110 });

    api.on('llm_output', async (event, ctx) => {
      if ((event?.provider || '') !== 'nvidia') return;
      const sessionKey = ctx?.sessionKey || '';
      const usage = event?.usage || {};
      try {
        await rmpFetch('POST', '/api/llm/record-gateway', {
          session_key: sessionKey,
          model: event?.model || '',
          input_tokens: usage.input || 0,
          output_tokens: usage.output || 0,
          total_tokens: usage.total || ((usage.input || 0) + (usage.output || 0)),
        });
      } catch (_) {}
    }, { priority: 50 });
  }
};
