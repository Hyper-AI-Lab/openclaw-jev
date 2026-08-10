const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execFileSync } = require('child_process');

const LOG = '/tmp/rmp_plugin_debug.log';
const RMP_API = 'http://127.0.0.1:8000';
const SETTINGS_PATH = '/root/.openclaw/rmp/settings.json';

function log(msg) {
  try { fs.appendFileSync(LOG, `[${new Date().toISOString()}] ${msg}\n`); } catch (_) {}
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

/** Synchronous RMP HTTP — before_message_write must not return a Promise. */
function rmpFetchSync(method, urlPath, body) {
  const key = getApiKey();
  const args = [
    '-sS',
    '-X', method,
    '-H', 'Content-Type: application/json',
    '-H', `X-RMP-API-Key: ${key}`,
    '--max-time', '10',
    '-w', '\n%{http_code}',
  ];
  if (body !== undefined) {
    args.push('-d', JSON.stringify(body));
  }
  args.push(`${RMP_API}${urlPath}`);
  const raw = execFileSync('/usr/bin/curl', args, { encoding: 'utf8', maxBuffer: 1024 * 1024 });
  const nl = raw.lastIndexOf('\n');
  const httpCode = raw.slice(nl + 1).trim();
  const text = raw.slice(0, nl);
  let data = null;
  try { data = text ? JSON.parse(text) : {}; } catch (_) { data = { raw: text }; }
  if (httpCode.startsWith('4') || httpCode.startsWith('5')) {
    throw new Error(data.detail || data.raw || `HTTP ${httpCode}`);
  }
  return data;
}

async function rmpFetch(method, urlPath, body) {
  return rmpFetchSync(method, urlPath, body);
}

function extractText(msg) {
  if (!msg?.content || !Array.isArray(msg.content)) return '';
  let text = '';
  for (const part of msg.content) {
    if (part?.type === 'text' && part.text) text += part.text;
  }
  return text;
}

function isStopCommand(intent) {
  return /\b(stop|abort|cancel|halt)\b/i.test(intent);
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

function createRmpTaskFromInbound({ sessionKey, intent, tags, rawText, heartbeatKey }) {
  let idemKey;
  if (heartbeatKey) {
    idemKey = crypto.createHash('sha256').update(`${sessionKey}:${heartbeatKey}`).digest('hex');
  } else {
    idemKey = crypto.createHash('sha256').update(`${sessionKey}:${rawText || intent}`).digest('hex');
  }
  const processHint = classifyProcessType(intent);
  const data = rmpFetchSync('POST', '/tasks', {
    intent: intent.substring(0, 500),
    tags: tags || ['user-request'],
    user_id: 'slack_user',
    session_key: sessionKey,
    raw_text: rawText || intent,
    process_type_hint: processHint,
    idempotency_key: idemKey,
  });
  if (data.skipped) {
    log(`INTAKE skipped: ${data.intake_action || 'skip'} — ${data.reason || ''}`);
    return data;
  }
  if (data.intake_action === 'wait_active') {
    log(`INTAKE wait_active on task ${data.task_id}`);
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
    prefetchProcessMemory(data.task_id, data.process_run_id);
  }
  const terminal = new Set(['failed', 'completed', 'stopped_by_user', 'cancelled']);
  if (data.deduplicated && terminal.has(data.status)) {
    log(`Prior task ${data.task_id} is ${data.status}; retrying`);
    const retried = rmpFetchSync('POST', `/tasks/${data.task_id}/retry`);
    log(`Retried as task ${retried.task_id}`);
  } else {
    log(`Created task ${data.task_id} (dedup=${!!data.deduplicated})`);
  }
  return data;
}

function routeSlackDmToRmp(content, sessionKey) {
  if (isDevSuspended()) {
    log('DEV MODE: Slack DM absorbed (no task, no delivery)');
    return true;
  }
  const intent = (content || '').trim();
  if (!intent) return true;

  if (isStopCommand(intent)) {
    try {
      const activeData = rmpFetchSync('GET', `/sessions/${encodeURIComponent(sessionKey)}/active_task`);
      if (activeData.active_task?.id) {
        rmpFetchSync('POST', `/tasks/${activeData.active_task.id}/signal`, {
          signal_type: 'user_input',
          message: intent,
        });
        log(`SIGNALED stop to task ${activeData.active_task.id}`);
      }
    } catch (e) {
      log(`Signal error: ${e.message}`);
      enableNativeSlackFallback(`stop-signal-failed:${e.message}`);
      throw e;
    }
    return true;
  }

  createRmpTaskFromInbound({
    sessionKey,
    intent,
    tags: ['user-request'],
    rawText: intent,
  });
  return true;
}

function isMainChatSession(sessionKey) {
  return sessionKey === 'agent:main:main' || (sessionKey || '').endsWith(':main');
}

function isRmpOwnedSlackSession(sessionKey) {
  const key = sessionKey || '';
  return isMainChatSession(key) || key.includes('slack:');
}

function getActiveRmpUserTask(sessionKey) {
  if (!isRmpOwnedSlackSession(sessionKey)) return null;
  try {
    const data = rmpFetchSync(
      'GET',
      `/sessions/${encodeURIComponent(sessionKey)}/active_user_task`
    );
    return data.active_task || null;
  } catch (_) {
    return null;
  }
}

function classifyProcessType(text) {
  // Fast pre-hint only; universal intake adjudicates final routing.
  const lower = (text || '').toLowerCase();
  if (/\b(register|sign up|create account)\b/.test(lower)) return 'account_registration';
  if (/\b(log in|login|sign in)\b/.test(lower)) return 'login';
  if (/\b(procure|purchase|buy)\b/.test(lower)) return 'procurement';
  if (/\b(email|follow-up|follow up)\b/.test(lower)) return 'email_followup';
  if (/\b(browser|navigate|automate|moltmarket)\b/.test(lower)) return 'browser_automation';
  return null;
}

function prefetchProcessMemory(taskId, processRunId) {
  if (!processRunId) return null;
  try {
    const ctx = rmpFetchSync('GET', `/memory/process/${encodeURIComponent(processRunId)}/context`);
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

function reserveLlmSlot(sessionKey) {
  const data = rmpFetchSync('POST', '/api/llm/reserve', { session_key: sessionKey });
  if (data.profile_id && sessionKey) {
    pinSessionProfile(sessionKey, data.profile_id);
  }
  return data;
}

function releaseLlmSlot(sessionKey) {
  try {
    rmpFetchSync('POST', '/api/llm/release', { session_key: sessionKey });
  } catch (e) {
    log(`Release slot failed for ${sessionKey}: ${e.message}`);
  }
}

function installNativeSlackSuppressor() {
  // OpenClaw Slack DM delivery (deliverReplies) bypasses message_sending hooks.
  // Patched dist calls this before chat.postMessage so RMP stays the sole reply path.
  // Exception: one-shot native fallback after RMP intake failure (avoid silent black hole).
  globalThis.__RMP_SUPPRESS_NATIVE_SLACK = (params) => {
    if (isDevSuspended()) return false;
    if (consumeNativeSlackFallback()) {
      const target = String(params?.target || '');
      log(`ALLOW native Slack deliverReplies after intake failure target=${target.slice(0, 40)}`);
      return false;
    }
    const target = String(params?.target || '');
    log(`SUPPRESSED native Slack deliverReplies (RMP owns delivery) target=${target.slice(0, 40)}`);
    return true;
  };
}

function isSlackInboundSession(sessionKey) {
  const key = sessionKey || '';
  return isMainChatSession(key) || key.includes('slack:');
}

/** When RMP intake fails, allow one native Slack reply so DMs are not silently dropped. */
let allowNativeSlackFallback = false;
function enableNativeSlackFallback(reason) {
  allowNativeSlackFallback = true;
  log(`Enabled native Slack fallback: ${reason}`);
}
function peekNativeSlackFallback() {
  return allowNativeSlackFallback;
}
function consumeNativeSlackFallback() {
  if (!allowNativeSlackFallback) return false;
  allowNativeSlackFallback = false;
  return true;
}

module.exports = {
  name: 'rmp_adapter',
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
      execute: async (params) => {
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

    api.registerTool({
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
      execute: async (params, context) => {
        try {
          const result = await rmpFetch('POST', '/tasks', {
            intent: params.intent,
            tags: params.tags || [],
            user_id: context?.session?.origin?.from || 'unknown',
            session_key: context?.sessionKey || 'agent:main:main',
            raw_text: params.intent
          });
          return `Task created: ${result.task_id}`;
        } catch (err) {
          return `Failed: ${err.message}`;
        }
      }
    });

    api.registerTool({
      name: 'rmp_task_status',
      description: 'Check the status of a task in the RMP.',
      parameters: {
        type: 'object',
        properties: { task_id: { type: 'string' } },
        required: ['task_id']
      },
      execute: async (params) => {
        try {
          const result = await rmpFetch('GET', `/tasks/${params.task_id}`);
          return `Task ${result.task_id}: ${result.status}`;
        } catch (err) {
          return `Failed: ${err.message}`;
        }
      }
    });

    // Runs before sendPolicy check — required so Slack DMs reach RMP even when
    // the main session must not deliver natively.
    api.on('message_received', (event, ctx) => {
      try {
        const meta = event?.metadata || {};
        const provider = String(meta.provider || meta.surface || meta.originatingChannel || ctx?.channelId || '').toLowerCase();
        const content = (event?.content || '').trim();
        if (!content) return;
        const isSlack = provider === 'slack' || provider.includes('slack');
        if (!isSlack) return;
        const sessionKey = ctx?.sessionKey || meta.sessionKey || 'agent:main:main';
        log(`message_received slack DM on ${sessionKey}: ${content.slice(0, 80)}`);
        routeSlackDmToRmp(content, sessionKey);
      } catch (e) {
        const sessionKey = ctx?.sessionKey || 'agent:main:main';
        log(`message_received route error: ${e.message}`);
        enableNativeSlackFallback(`intake-route-failed:${e.message}`);
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
          if (peekNativeSlackFallback()) {
            log('ALLOW Slack-session assistant msg (native fallback after intake failure)');
            return;
          }
          if (isPureSystemAck(text)) {
            log('BLOCKED system ack from Slack session transcript');
            return { block: true };
          }
          if (!isDevSuspended()) {
            log(`BLOCKED Slack-session assistant msg (RMP owns delivery)`);
            return { block: true };
          }
          const active = getActiveRmpUserTask(ctx?.sessionKey || 'agent:main:main');
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

        // Internal heartbeats must not spawn RMP workflows (they were flooding the worker).
        if (isHeartbeat && !isSlackDM) {
          log('SKIP RMP routing for internal heartbeat');
          return { block: true };
        }

        if (isSlackDM) {
          if (peekNativeSlackFallback()) {
            log('ALLOW Slack DM write after intake failure (native fallback)');
            return;
          }
          log('BLOCKED Slack DM write on main session (RMP routed via message_received)');
          return { block: true };
        }

        const sessionKey = ctx?.sessionKey || 'agent:main:main';

        if ((isHeartbeat || isCron) && !isSlackDM) {
          try {
            const activeData = rmpFetchSync(
              'GET',
              `/sessions/${encodeURIComponent(sessionKey)}/active_task`
            );
            if (activeData.active_task?.id) {
              log(`SKIP heartbeat/cron — active task ${activeData.active_task.id} on ${sessionKey}`);
              return { block: true };
            }
          } catch (e) {
            log(`Heartbeat active check error: ${e.message}`);
          }
        }

        let intent = text;
        if (isCron) {
          intent = text.replace(/^\[cron:[^\]]*\]\s*/, '').trim();
        }

        if (isCron || isHeartbeat) {
          try {
            let idemKey;
            if (isHeartbeat || (isCron && sessionKey.includes('heartbeat'))) {
              idemKey = 'heartbeat-v1';
            } else {
              idemKey = null;
            }
            createRmpTaskFromInbound({
              sessionKey,
              intent: intent.substring(0, 500),
              tags: isCron ? ['cron'] : isHeartbeat ? ['heartbeat'] : ['user-request'],
              rawText: intent,
              heartbeatKey: idemKey,
            });
          } catch (e) {
            log(`Task creation FAILED (fail-closed): ${e.message}`);
          }
          return { block: true };
        }
      } catch (e) {
        log(`Hook error: ${e.message}`);
        return { block: true };
      }
    }, { priority: 100 });

    api.on('message_sending', (event, ctx) => {
      const content = event?.content || '';
      if (!content.trim()) return { cancel: true };

      const sessionKey = ctx?.sessionKey || event?.sessionKey || 'agent:main:main';

      // RMP owns all Slack DM delivery; native gateway must never double-post.
      if (isSlackInboundSession(sessionKey) && !isDevSuspended()) {
        if (peekNativeSlackFallback()) {
          log(`ALLOW native Slack on ${sessionKey} (intake failure fallback)`);
          return;
        }
        log(`SUPPRESSED native Slack on ${sessionKey} (RMP owns delivery)`);
        return { cancel: true };
      }

      const active = getActiveRmpUserTask(sessionKey);
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

    // Balanced NVIDIA key rotation + concurrency cap for gateway agent runs.
    // RMP-owned sessions (rmp_task_*/rmp_verify_*/rmp_intake_*) reserve/release inside the worker.
    api.on('before_agent_start', async (event, ctx) => {
      const sessionKey = ctx?.sessionKey || '';
      const trigger = ctx?.trigger || '';
      if (!sessionKey || sessionKey.includes('rmp_task_') || sessionKey.includes('rmp_verify_') || sessionKey.includes('rmp_intake_')) {
        return;
      }
      if (trigger === 'heartbeat') {
        log(`SKIP LLM reserve for heartbeat on ${sessionKey}`);
        return;
      }
      if (isRmpOwnedSlackSession(sessionKey) && !isDevSuspended()) {
        log(`SKIP LLM reserve on Slack/main session (RMP owns Slack path): ${sessionKey}`);
        return;
      }
      try {
        const data = await rmpFetch('POST', '/api/llm/reserve', { session_key: sessionKey });
        log(`Reserved ${data.profile_id} for ${sessionKey} (${data.orchestration?.active_slots || '?'}/${data.orchestration?.max_concurrent || '?'} slots)`);
      } catch (e) {
        log(`LLM reserve failed for ${sessionKey}: ${e.message}`);
      }
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
