'use strict';

const { test, mock, beforeEach, afterEach } = require('node:test');
// The plugin talks to the live RMP API on this host. Background routes can outlive a
// failed test, so the real fetch and real settings are never put back in this process.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const PLUGIN = path.resolve(__dirname, '../../plugins/rmp_adapter/index.js');
const LIVE_PLUGIN = '/root/.openclaw/plugins/rmp_adapter/index.js';
const LOG_PATH = '/root/.openclaw/logs/rmp_adapter.log';
const SETTINGS_PATH = '/root/.openclaw/rmp/settings.json';
const SESSIONS_JSON = '/root/.openclaw/agents/main/sessions/sessions.json';
const AGENT_SQLITE = '/root/.openclaw/agents/main/agent/openclaw-agent.sqlite';
const SLACK_KEY = 'agent:main:slack:channel:dtest';

const realReadFileSync = fs.readFileSync.bind(fs);
const realAppendFileSync = fs.appendFileSync.bind(fs);
const realWriteFileSync = fs.writeFileSync.bind(fs);

let logs = [];
let writes = [];
let sessionsStore = null;

function networkDisabled(url) {
  return Promise.reject(new Error(`network disabled in tests: ${url}`));
}
globalThis.fetch = networkDisabled;

mock.method(fs, 'readFileSync', (file, ...rest) => {
  if (file === SETTINGS_PATH) {
    return JSON.stringify({
      api_key: 'test-key',
      task_registry: { intake_llm_timeout_sec: 40, intake_vector_deadline_sec: 10 },
    });
  }
  if (file === SESSIONS_JSON) {
    if (!sessionsStore) {
      const err = new Error(`ENOENT: no such file or directory, open '${file}'`);
      err.code = 'ENOENT';
      throw err;
    }
    return JSON.stringify(sessionsStore);
  }
  return realReadFileSync(file, ...rest);
});
mock.method(fs, 'appendFileSync', (file, data, ...rest) => {
  if (file === LOG_PATH) {
    logs.push(String(data));
    return undefined;
  }
  return realAppendFileSync(file, data, ...rest);
});
mock.method(fs, 'writeFileSync', (file, data, ...rest) => {
  if (String(file).startsWith('/root/') || String(file).startsWith('/tmp/rmp_ctx_')) {
    writes.push(String(file));
    return undefined;
  }
  return realWriteFileSync(file, data, ...rest);
});

function sha256(text) {
  return crypto.createHash('sha256').update(text).digest('hex');
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function timeoutError() {
  return new DOMException('The operation was aborted due to timeout', 'TimeoutError');
}

function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

async function waitFor(pred, what) {
  for (let i = 0; i < 2000; i += 1) {
    if (pred()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  throw new Error(`timed out waiting for ${what}`);
}

function sawLog(fragment) {
  return logs.some((line) => line.includes(fragment));
}

/** Route table: [["POST /tasks", handler], [/^GET \/tasks\/by-idempotency\//, handler]]. */
function installFetch(routes) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    const key = `${init.method || 'GET'} ${new URL(url).pathname}`;
    const call = { key, init, body: init.body ? JSON.parse(init.body) : undefined };
    calls.push(call);
    for (const [pattern, handler] of routes) {
      if (pattern instanceof RegExp ? pattern.test(key) : pattern === key) {
        const out = await handler(call);
        return out instanceof Response ? out : json(out);
      }
    }
    return json({ detail: `no route for ${key}` }, 404);
  };
  return calls;
}

function loadPlugin() {
  delete require.cache[require.resolve(PLUGIN)];
  const plugin = require(PLUGIN);
  const hooks = {};
  const tools = {};
  plugin.register({
    on: (name, handler) => { hooks[name] = handler; },
    registerTool: (tool) => {
      const built = typeof tool === 'function' ? tool({ sessionKey: SLACK_KEY }) : tool;
      tools[built.name] = built;
    },
  });
  return { hooks, tools };
}

function text(result) {
  return result.content.map((part) => part.text).join('');
}

function slackDm(content) {
  return [
    { content, metadata: { provider: 'slack' } },
    { channelId: 'slack', sessionKey: SLACK_KEY },
  ];
}

beforeEach(() => {
  logs = [];
  writes = [];
  sessionsStore = { [SLACK_KEY]: {} };
});

afterEach(() => {
  mock.timers.reset();
  globalThis.fetch = networkDisabled;
});

test('message_received claims at once while POST /tasks is still in intake', async () => {
  const post = deferred();
  const calls = installFetch([['POST /tasks', () => post.promise]]);
  const { hooks } = loadPlugin();

  const claimed = hooks.message_received(...slackDm('hello aura'));
  assert.deepEqual(claimed, { handled: true });
  await waitFor(() => calls.length === 1, 'POST /tasks');

  let loopRan = false;
  setImmediate(() => { loopRan = true; });
  await waitFor(() => loopRan, 'event loop turn during intake');

  const [event, ctx] = slackDm('hello aura');
  assert.deepEqual(hooks.before_dispatch({ ...event, channel: 'slack' }, ctx), { handled: true });

  post.resolve({ task_id: 't1', status: 'created' });
  await waitFor(() => sawLog('Created task t1'), 'task creation log');
  assert.equal(calls.length, 1, 'before_dispatch must not POST the claimed DM again');
  const [call] = calls;
  assert.equal(call.body.session_key, SLACK_KEY);
  assert.equal(call.body.raw_text, 'hello aura');
  assert.equal(call.body.idempotency_key, sha256(`${SLACK_KEY}:hello aura`));
  assert.equal(call.init.headers['X-RMP-API-Key'], 'test-key');
  assert.ok(call.init.signal instanceof AbortSignal);
});

test('intake timeout recovers through an idempotent re-POST after an async sleep', async () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  let attempts = 0;
  const calls = installFetch([
    ['POST /tasks', () => {
      attempts += 1;
      if (attempts === 1) throw timeoutError();
      return { task_id: 't2', status: 'created', deduplicated: true };
    }],
  ]);
  const { hooks } = loadPlugin();

  assert.deepEqual(hooks.message_received(...slackDm('long request')), { handled: true });
  await waitFor(() => sawLog('Intake POST timed out'), 'timeout log');
  assert.equal(attempts, 1, 're-POST waits for the 2 s sleep');
  mock.timers.tick(2000);
  await waitFor(() => sawLog('Recovered RMP ownership after timeout (task=t2)'), 'recovery');
  assert.equal(calls.filter((c) => c.key === 'POST /api/notify-user').length, 0);
});

test('intake that never recovers sends the intake_unavailable notice', async () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  const calls = installFetch([
    ['POST /tasks', () => { throw timeoutError(); }],
    [/^GET \/tasks\/by-idempotency\//, () => json({ detail: 'not found' }, 404)],
    ['POST /api/notify-user', () => ({ delivered: true })],
  ]);
  const { hooks } = loadPlugin();

  assert.deepEqual(hooks.message_received(...slackDm('never lands')), { handled: true });
  await waitFor(() => sawLog('Intake POST timed out'), 'timeout log');
  for (const [step, waitMs] of [[1, 2000], [2, 3000], [3, 5000]]) {
    mock.timers.tick(waitMs);
    await waitFor(
      () => calls.filter((c) => c.key.startsWith('GET /tasks/by-idempotency/')).length === step,
      `idempotency lookup ${step}`,
    );
  }
  await waitFor(() => sawLog('message_received route error (claimed; no native)'), 'route error');
  const notices = calls.filter((c) => c.key === 'POST /api/notify-user');
  assert.equal(notices.length, 1);
  assert.equal(notices[0].body.reason, 'intake_unavailable');
  assert.equal(notices[0].body.session_key, SLACK_KEY);
  const idem = calls.find((c) => c.key.startsWith('GET /tasks/by-idempotency/'));
  assert.ok(idem.key.endsWith(sha256(`${SLACK_KEY}:never lands`)));
});

test('a non-timeout intake failure notifies once without re-POSTing', async () => {
  const calls = installFetch([
    ['POST /tasks', () => json({ detail: 'boom' }, 500)],
    ['POST /api/notify-user', () => ({ delivered: true })],
  ]);
  const { hooks } = loadPlugin();

  assert.deepEqual(hooks.message_received(...slackDm('server error')), { handled: true });
  await waitFor(() => sawLog('route error (claimed; no native): HTTP 500: boom'), 'route error');
  assert.equal(calls.filter((c) => c.key === 'POST /tasks').length, 1);
  const notices = calls.filter((c) => c.key === 'POST /api/notify-user');
  assert.deepEqual(notices.map((c) => c.body.reason), ['intake_unavailable']);
});

test('stop signals the active task, and an idle stop gets the stop_idle notice', async () => {
  let calls = installFetch([
    [/^GET \/sessions\/.+\/active_user_task$/, () => ({ active_task: { id: 'a1' } })],
    ['POST /tasks/a1/signal', () => ({ ok: true })],
  ]);
  let { hooks } = loadPlugin();
  assert.deepEqual(hooks.message_received(...slackDm('stop')), { handled: true });
  await waitFor(() => sawLog('SIGNALED stop to task a1'), 'stop signal');
  assert.deepEqual(calls.map((c) => c.key), [
    `GET /sessions/${encodeURIComponent(SLACK_KEY)}/active_user_task`,
    'POST /tasks/a1/signal',
  ]);
  assert.deepEqual(calls[1].body, { signal_type: 'user_input', message: 'stop' });

  calls = installFetch([
    [/^GET \/sessions\/.+\/active_user_task$/, () => ({ active_task: null })],
    ['POST /api/notify-user', () => ({ delivered: true })],
  ]);
  ({ hooks } = loadPlugin());
  assert.deepEqual(hooks.message_received(...slackDm('Stop.')), { handled: true });
  await waitFor(() => sawLog('Stop with no active task; RMP ack'), 'stop_idle ack');
  const notices = calls.filter((c) => c.key === 'POST /api/notify-user');
  assert.deepEqual(notices.map((c) => c.body.reason), ['stop_idle']);
});

test('DMs on one session reach intake one at a time, in arrival order', async () => {
  const first = deferred();
  const calls = installFetch([
    ['POST /tasks', (call) => (call.body.raw_text === 'first'
      ? first.promise
      : { task_id: 't-second', status: 'created' })],
  ]);
  const { hooks } = loadPlugin();

  assert.deepEqual(hooks.message_received(...slackDm('first')), { handled: true });
  assert.deepEqual(hooks.message_received(...slackDm('second')), { handled: true });
  await waitFor(() => calls.length === 1, 'first POST');
  for (let i = 0; i < 50; i += 1) await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.length, 1, 'second DM waits for the first intake');

  first.resolve({ task_id: 't-first', status: 'created' });
  await waitFor(() => sawLog('Created task t-second'), 'second task');
  assert.deepEqual(calls.map((c) => c.body.raw_text), ['first', 'second']);
});

test('before_message_write answers synchronously and routes cron in the background', async () => {
  const activeCheck = deferred();
  const calls = installFetch([
    [/^GET \/sessions\/.+\/active_task$/, () => activeCheck.promise],
    ['POST /tasks', () => ({ task_id: 'c1', status: 'created' })],
  ]);
  const { hooks } = loadPlugin();

  const cron = hooks.before_message_write(
    { message: { role: 'user', content: [{ type: 'text', text: '[cron:job-1] summarize inbox' }] } },
    { sessionKey: 'agent:main:cron:job-1' },
  );
  assert.deepEqual(cron, { block: true }, 'a plain result, not a Promise, while the check is pending');
  activeCheck.resolve({ active_task: null });
  await waitFor(() => sawLog('Created task c1'), 'cron task');
  assert.deepEqual(calls.map((c) => c.key), [
    `GET /sessions/${encodeURIComponent('agent:main:cron:job-1')}/active_task`,
    'POST /tasks',
  ]);
  assert.deepEqual(calls[1].body.tags, ['cron']);
  assert.equal(calls[1].body.raw_text, 'summarize inbox');

  const assistant = hooks.before_message_write(
    { message: { role: 'assistant', content: [{ type: 'text', text: 'native reply' }] } },
    { sessionKey: SLACK_KEY },
  );
  assert.deepEqual(assistant, { block: true });
});

test('message_sending cancels native Slack delivery', async () => {
  const calls = installFetch([]);
  const { hooks } = loadPlugin();
  assert.deepEqual(await hooks.message_sending({ content: 'hi' }, { sessionKey: SLACK_KEY }), { cancel: true });
  assert.equal(calls.length, 0);
});

test('a route that outlives its test cannot reach the live API', async () => {
  const activeCheck = deferred();
  installFetch([[/^GET \/sessions\/.+\/active_task$/, () => activeCheck.promise]]);
  const { hooks } = loadPlugin();
  hooks.before_message_write(
    { message: { role: 'user', content: [{ type: 'text', text: '[cron:job-2] leak probe' }] } },
    { sessionKey: 'agent:main:cron:job-2' },
  );
  globalThis.fetch = networkDisabled;
  activeCheck.resolve({ active_task: null });
  await waitFor(
    () => sawLog('Task creation FAILED (fail-closed): network disabled in tests'),
    'POST /tasks after the test ended',
  );
  assert.equal(JSON.parse(fs.readFileSync(SETTINGS_PATH, 'utf8')).api_key, 'test-key');
});

test('plugin never spawns processes, and the live copy matches the repo', () => {
  for (const file of [PLUGIN, path.join(path.dirname(PLUGIN), 'claude_tools.js')]) {
    const src = realReadFileSync(file, 'utf8');
    assert.doesNotMatch(src, /child_process|execFileSync|execSync|spawnSync/);
    if (fs.existsSync(LIVE_PLUGIN)) {
      assert.equal(realReadFileSync(path.join(path.dirname(LIVE_PLUGIN), path.basename(file)), 'utf8'), src);
    }
  }
});

test('every tool the plugin registers is declared in its manifest, which OpenClaw requires', () => {
  const manifest = JSON.parse(realReadFileSync(path.join(path.dirname(PLUGIN), 'openclaw.plugin.json'), 'utf8'));
  assert.deepEqual(Object.keys(loadPlugin().tools).sort(), [...manifest.contracts.tools].sort());
  if (fs.existsSync(LIVE_PLUGIN)) {
    assert.deepEqual(JSON.parse(realReadFileSync(path.join(path.dirname(LIVE_PLUGIN), 'openclaw.plugin.json'), 'utf8')), manifest);
  }
});

test('claude tools start a session, send a message, wait across long polls and end it', async () => {
  let polls = 0;
  const calls = installFetch([
    ['POST /api/claude/sessions', () => ({ id: 's-1', workspace: 'repo', path: '/srv/aura-code/direct/t/s-1/repo' })],
    ['POST /api/claude/sessions/s-1/messages', () => ({ session: 's-1', turn: 2 })],
    ['GET /api/claude/sessions/s-1/turns/2', () => ((polls += 1) < 3
      ? { turn: 2, done: false, outcome: 'running', progress: ['Read README.md'] }
      : { turn: 2, done: true, outcome: 'success', reply: 'Fixed add().', files_edited: ['calc.py'],
        commands: ['pytest -q'], denied: [] })],
    ['POST /api/claude/sessions/s-1/end', () => ({ id: 's-1', turns: 2, stopped: [] })],
  ]);
  const { tools } = loadPlugin();
  const started = await tools.claude_start.execute('c1', { title: 'Fix calc' });
  assert.match(text(started), /^Claude session s-1 is ready \(repo: /);
  assert.deepEqual(started.details, { session: 's-1', workspace: 'repo', path: '/srv/aura-code/direct/t/s-1/repo' });
  const answer = await tools.claude_send.execute('c2', { session: 's-1', message: 'Fix add()' });
  assert.equal(text(answer), 'Claude, turn 2: success\n\nFixed add().\n\nFiles edited: calc.py\nCommands (last 1): pytest -q');
  assert.deepEqual(answer.details, { session: undefined, turn: 2, done: true, outcome: 'success' });
  assert.equal(polls, 3);
  assert.deepEqual(calls[0].body, { session_key: SLACK_KEY, workspace: 'repo', title: 'Fix calc' });
  assert.deepEqual(calls[1].body, { message: 'Fix add()' });
  assert.equal(text(await tools.claude_end.execute('c3', { session: 's-1' })), 'Claude session s-1 ended after 2 turn(s).');
});

test('a tool result is an object OpenClaw can read in a script, never a bare string', async () => {
  installFetch([['POST /api/claude/sessions/s-1/messages', () => json({ detail: 'the session has ended' }, 409)]]);
  const { tools } = loadPlugin();
  const failed = await tools.claude_send.execute('c1', { session: 's-1', message: 'go' });
  assert.ok('details' in failed && Array.isArray(failed.content));
  assert.equal(text(failed), 'Claude message failed: HTTP 409: the session has ended');
});

test('a Claude wait rides out an RMP restart, and a refused message or a usage limit is reported', async () => {
  let polls = 0;
  installFetch([
    ['POST /api/claude/sessions/s-1/messages', () => ({ session: 's-1', turn: 1 })],
    ['GET /api/claude/sessions/s-1/turns/1', () => {
      polls += 1;
      if (polls === 1) throw new TypeError('fetch failed');
      return { turn: 1, done: true, outcome: 'usage_limit', reply: '', resets_at: 1790000000 };
    }],
    ['POST /api/claude/sessions/s-2/messages', () => json({ detail: 'turn 3 is still running' }, 409)],
    ['GET /api/claude/sessions/s-3', () => ({ id: 's-3', status: 'open', turns: 1 })],
    ['GET /api/claude/sessions/s-3/turns/1', () => ({ turn: 1, done: false, outcome: 'running',
      progress: ['Ran: pytest', 'Edited calc.py'], files_edited: ['calc.py'], commands: ['pytest -q', 'git diff'] })],
  ]);
  const tools = {};
  require(path.join(path.dirname(PLUGIN), 'claude_tools.js')).register(
    { registerTool: (tool) => { const built = typeof tool === 'function' ? tool({}) : tool; tools[built.name] = built; } },
    { rmpFetch: require(PLUGIN).rmpFetch, pause: async () => {} },
  );
  assert.equal(text(await tools.claude_send.execute('c1', { session: 's-1', message: 'go' })),
    "Claude, turn 1: usage_limit\n\n(no reply)\nClaude's usage limit is reached; it resets at 2026-09-21T14:13:20.000Z.");
  assert.equal(polls, 2);
  assert.equal(text(await tools.claude_send.execute('c2', { session: 's-2', message: 'again' })),
    'Claude message failed: HTTP 409: turn 3 is still running');
  assert.equal(text(await tools.claude_status.execute('c3', { session: 's-3', wait_seconds: 1 })),
    'Claude is still working on turn 1.\nLatest: Ran: pytest; Edited calc.py\nWait for it with claude_status, or stop it with claude_end.');
});

test('session lookup reads the OpenClaw SQLite store in-process', {
  skip: !fs.existsSync(AGENT_SQLITE) && 'no OpenClaw store on this host',
}, async () => {
  const { DatabaseSync } = require('node:sqlite');
  const db = new DatabaseSync(AGENT_SQLITE, { readOnly: true });
  const keys = db.prepare("select session_key from session_nodes where session_key like '%slack:channel:%'")
    .all()
    .map((row) => String(row.session_key));
  db.close();
  const sortKey = (k) => `${k.toLowerCase().includes('slack:channel:d') ? 0 : 1}${k.toLowerCase()}`;
  keys.sort((a, b) => (sortKey(a) < sortKey(b) ? -1 : sortKey(a) > sortKey(b) ? 1 : 0));

  sessionsStore = null;
  const calls = installFetch([['POST /tasks', () => ({ task_id: 's1', status: 'created' })]]);
  const { hooks } = loadPlugin();
  assert.deepEqual(hooks.before_dispatch({ channel: 'slack', content: 'no key on event' }, { channelId: 'slack' }), { handled: true });
  await waitFor(() => sawLog('Created task s1'), 'task from store key');
  assert.equal(calls[0].body.session_key, keys[0] || 'agent:main:main');
});

test('identical texts with different Slack ids are two messages; one id seen twice is one', async () => {
  const calls = installFetch([['POST /tasks', (call) => ({ task_id: `t-${call.body.slack_message_id}`, status: 'created' })]]);
  const { hooks } = loadPlugin();
  const dm = (messageId) => [{ content: 'ok', messageId, metadata: { provider: 'slack' } }, { channelId: 'slack', sessionKey: SLACK_KEY }];

  assert.deepEqual(hooks.message_received(...dm('1790000000.000100')), { handled: true });
  assert.deepEqual(hooks.message_received(...dm('1790000060.000200')), { handled: true });
  const [again, ctx] = dm('1790000060.000200');
  assert.deepEqual(hooks.before_dispatch({ ...again, channel: 'slack' }, ctx), { handled: true });
  await waitFor(() => calls.length === 2, 'two POST /tasks');
  await waitFor(() => sawLog('Created task t-1790000060.000200'), 'second task');
  assert.equal(calls.length, 2, 'the second hook must not re-post the same Slack message');
  assert.deepEqual(calls.map((c) => c.body.idempotency_key), [
    sha256(`${SLACK_KEY}:msg:1790000000.000100`),
    sha256(`${SLACK_KEY}:msg:1790000060.000200`),
  ]);
});

test('an attachment-only DM is claimed and reaches intake with its file', async () => {
  const calls = installFetch([['POST /tasks', () => ({ task_id: 't-file', status: 'created' })]]);
  const { hooks } = loadPlugin();
  const event = {
    content: '',
    messageId: '1790000100.000300',
    media: [{ path: '/root/.openclaw/media/inbound/report.pdf', contentType: 'application/pdf' }],
    metadata: { provider: 'slack' },
  };
  assert.deepEqual(hooks.message_received(event, { channelId: 'slack', sessionKey: SLACK_KEY }), { handled: true });
  await waitFor(() => calls.length === 1, 'POST /tasks');
  const { body } = calls[0];
  assert.match(body.raw_text, /^Kirill sent an attachment\.\n\n\[Attachments\]\n- report\.pdf \(application\/pdf\): \/root\/\.openclaw\/media\/inbound\/report\.pdf$/);
  assert.deepEqual(body.attachments, [{ path: '/root/.openclaw/media/inbound/report.pdf', type: 'application/pdf', name: 'report.pdf' }]);
});

test('reply-to and thread ids travel with the message', async () => {
  const calls = installFetch([['POST /tasks', () => ({ task_id: 't-reply', status: 'created' })]]);
  const { hooks } = loadPlugin();
  const event = {
    content: 'yes, go ahead with that one',
    messageId: '1790000200.000400',
    threadId: '1790000150.000350',
    replyToId: '1790000150.000350',
    replyToBody: 'Shall I book the 10:05 train?',
    metadata: { provider: 'slack' },
  };
  hooks.message_received(event, { channelId: 'slack', sessionKey: SLACK_KEY });
  await waitFor(() => calls.length === 1, 'POST /tasks');
  assert.equal(calls[0].body.thread_id, '1790000150.000350');
  assert.deepEqual(calls[0].body.reply_to, { id: '1790000150.000350', body: 'Shall I book the 10:05 train?', sender: '' });
});

test('the Slack sender and event time travel with the message, for approval checks', async () => {
  const calls = installFetch([['POST /tasks', () => ({ task_id: 't-sender', status: 'created' })]]);
  const { hooks } = loadPlugin();
  const event = {
    content: 'approve',
    messageId: '1790000300.000500',
    senderId: 'U0AELFYTLKS',
    timestamp: 1790000300123,
    metadata: { provider: 'slack' },
  };
  hooks.message_received(event, { channelId: 'slack', sessionKey: SLACK_KEY });
  await waitFor(() => calls.length === 1, 'POST /tasks');
  assert.equal(calls[0].body.slack_user_id, 'U0AELFYTLKS');
  assert.equal(calls[0].body.slack_event_ts, 1790000300123);
  assert.equal(calls[0].body.slack_message_id, '1790000300.000500');
});

test('a structured API error is logged readably, not as [object Object]', async () => {
  installFetch([
    ['POST /tasks', () => json({ detail: { intake_action: 'attach_active', error: 'workflow not found' } }, 502)],
    ['POST /api/notify-user', () => ({ delivered: true })],
  ]);
  const { hooks } = loadPlugin();
  hooks.message_received(...slackDm('hey'));
  await waitFor(() => sawLog('route error (claimed; no native): HTTP 502: {"intake_action":"attach_active","error":"workflow not found"}'), 'readable error');
  assert.ok(!sawLog('[object Object]'));
});

test('the plugin log never carries the RMP key or an auth header value', async () => {
  installFetch([['POST /tasks', () => json({ detail: 'X-RMP-API-Key: test-key rejected; Authorization: Bearer abc.def' }, 401)],
    ['POST /api/notify-user', () => ({ delivered: true })]]);
  const { hooks } = loadPlugin();
  hooks.message_received(...slackDm('hi'));
  await waitFor(() => sawLog('route error'), 'route error');
  const text = logs.join('');
  assert.ok(!text.includes('test-key') && !text.includes('abc.def'), text);
  assert.ok(text.includes('X-RMP-API-Key: ***') && text.includes('Bearer ***'));
});

test('plugin tools read their arguments the way OpenClaw passes them: execute(toolCallId, params)', async () => {
  const calls = installFetch([
    [/^GET \/memory\/process\/pr-1\/context$/, () => ({ context_block: 'Kirill prefers metric units.', count: 1 })],
    ['POST /tasks', () => ({ task_id: 't-new', status: 'created' })],
    ['GET /tasks/t-9', () => ({ task_id: 't-9', status: 'running' })],
  ]);
  const { tools } = loadPlugin();
  assert.equal(
    await tools.rmp_memory_recall.execute('call-1', { process_run_id: 'pr-1', query: 'units' }),
    'PROCESS-SCOPED MEMORY (1 items):\nKirill prefers metric units.'
  );
  assert.equal(await tools.rmp_task_create.execute('call-2', { intent: 'Draft the memo', task_type: 'user' }), 'Task created: t-new');
  assert.equal(await tools.rmp_task_status.execute('call-3', { task_id: 't-9' }), 'Task t-9: running');
  assert.equal(calls[0].key, 'GET /memory/process/pr-1/context');
  assert.equal(calls[1].body.intent, 'Draft the memo');
  assert.equal(calls[1].body.session_key, SLACK_KEY);
});

test('an RMP run cannot wait on a tool prompt Kirill never sees', () => {
  const { hooks } = loadPlugin();
  const call = (toolName, params, sessionKey) => hooks.before_tool_call({ toolName, params }, { sessionKey });
  for (const session of ['agent:main:rmp_task_t1', 'agent:main:rmp_verify_t1', 'agent:main:rmp_intake_abc']) {
    const blocked = call('ask_user', { questions: [] }, session);
    assert.equal(blocked.block, true);
    assert.match(blocked.blockReason, /Put the question in your reply/);
    assert.equal(call('secrets', { action: 'request', name: 'TEST_CODE_WORD' }, session).block, true);
    assert.equal(call('secrets', { action: 'list' }, session), undefined);
    assert.equal(call('web_fetch', { url: 'https://example.org' }, session), undefined);
  }
  assert.equal(call('ask_user', { questions: [] }, 'agent:main:main'), undefined);
});
