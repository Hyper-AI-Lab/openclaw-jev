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
  assert.deepEqual(started.details, { session: 's-1', workspace: 'repo', path: '/srv/aura-code/direct/t/s-1/repo', text: text(started) });
  const answer = await tools.claude_send.execute('c2', { session: 's-1', message: 'Fix add()' });
  assert.equal(text(answer), 'Claude, turn 2: success\n\nFixed add().\n\nFiles edited: calc.py\nCommands (last 1): pytest -q');
  assert.deepEqual(answer.details, { session: undefined, turn: 2, done: true, outcome: 'success', text: text(answer) });
  assert.equal(polls, 3);
  assert.deepEqual(calls[0].body, { session_key: SLACK_KEY, workspace: 'repo', title: 'Fix calc' });
  assert.deepEqual(calls[1].body, { message: 'Fix add()', plan: false });
  assert.equal(text(await tools.claude_end.execute('c3', { session: 's-1' })), 'Claude session s-1 ended after 2 turn(s).');
});

test('a planning turn asks RMP for plan mode and an effort level, and says it was planning', async () => {
  const calls = installFetch([
    ['POST /api/claude/sessions/s-1/messages', () => ({ session: 's-1', turn: 1 })],
    ['GET /api/claude/sessions/s-1/turns/1', () => ({ turn: 1, done: true, outcome: 'success', plan: true, reply: '1. Edit calc.py' })],
  ]);
  const { tools } = loadPlugin();
  const answer = await tools.claude_send.execute('c1', { session: 's-1', message: 'Plan the fix', plan: true, effort: 'high' });
  assert.deepEqual(calls[0].body, { message: 'Plan the fix', plan: true, effort: 'high' });
  assert.equal(text(answer), 'Claude, turn 1 (planning): success\n\n1. Edit calc.py');
});

test('attach_file asks RMP to send a file of a Claude session with her accepted reply', async () => {
  const where = '/srv/aura-code/direct/t/s/scratch/out.csv';
  const calls = installFetch([
    ['POST /api/replies/files', () => ({ id: 'f1', name: 'out.csv', size: 8, title: 'The sheet' })],
  ]);
  const { tools } = loadPlugin();
  const attached = await tools.attach_file.execute('c1', { path: where, title: 'The sheet' });
  assert.equal(text(attached), 'Attached out.csv (8 bytes): it goes to Kirill with your reply.');
  assert.deepEqual(calls[0].body, { session_key: SLACK_KEY, path: where, title: 'The sheet' });
});

test('a tool result is an object OpenClaw can read in a script, never a bare string', async () => {
  installFetch([['POST /api/claude/sessions/s-1/messages', () => json({ detail: 'the session has ended' }, 409)]]);
  const { tools } = loadPlugin();
  const failed = await tools.claude_send.execute('c1', { session: 's-1', message: 'go' });
  assert.ok('details' in failed && Array.isArray(failed.content));
  // A script that prints the result sees only its details: Claude's answer must be in them.
  assert.equal(failed.details.text, 'Claude message failed: HTTP 409: the session has ended');
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

// ---- exec result cap: a task session's exec output never holds more than 12,000 characters ----

const os = require('node:os');

const CAP_MODULE = path.resolve(__dirname, '../../plugins/rmp_adapter/exec_result_cap.js');
const TASK_UUID = '3f2a9c1e-5b7d-4e8a-9c0b-1d2e3f4a5b6c';
const TASK_KEY = `agent:main:rmp_task_${TASK_UUID}`;
const MAX = 12000;
const DAY_MS = 24 * 3600 * 1000;

function capModule() {
  delete require.cache[CAP_MODULE];
  return require(CAP_MODULE);
}

/** Deterministic output of exactly n characters, one numbered line at a time. */
function output(n, { marker } = {}) {
  let out = '';
  let i = 0;
  let marked = !marker;
  while (out.length < n) {
    if (!marked && out.length >= n / 2) {
      out += `${marker}\n`;
      marked = true;
      continue;
    }
    out += `line ${String(i++).padStart(6, '0')} ${'x'.repeat(40)}\n`;
  }
  return out.slice(0, n);
}

function execResult(textValue, details) {
  return {
    content: [{ type: 'text', text: textValue }],
    ...(details === undefined ? {} : { details }),
  };
}

function execDetails(extra = {}) {
  return { cwd: '/w', durationMs: 5, exitCode: 0, exitReason: 'exit', exitSignal: null, noOutputTimedOut: false, status: 'completed', ...extra };
}

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'rmp-cap-'));
}

/** A cap handler writing into a fresh private directory, plus the directory it uses. */
function newCap(options = {}) {
  const base = tmpDir();
  const dir = options.dir || path.join(base, 'exec-results');
  const { createExecResultCap } = capModule();
  const handler = createExecResultCap({ dir, ownerUid: process.geteuid(), ...options.opts });
  return { base, dir, handler, run: (result, key = TASK_KEY, tool = 'exec') => handler({ toolName: tool, result, isError: false }, { sessionKey: key, runtime: 'openclaw' }) };
}

function cappedText(out) {
  assert.ok(out && out.result, 'a capped result was returned');
  return out.result.content[0].text;
}

test('exec cap: the manifest declares the middleware contract and the plugin registers it for exec on openclaw', () => {
  const manifest = JSON.parse(realReadFileSync(path.join(path.dirname(PLUGIN), 'openclaw.plugin.json'), 'utf8'));
  assert.deepEqual(manifest.contracts.agentToolResultMiddleware, ['openclaw']);

  delete require.cache[require.resolve(PLUGIN)];
  const registered = [];
  require(PLUGIN).register({
    on: () => {},
    registerTool: () => {},
    registerAgentToolResultMiddleware: (handler, options) => registered.push({ handler, options }),
  });
  assert.equal(registered.length, 1);
  assert.equal(typeof registered[0].handler, 'function');
  assert.deepEqual(registered[0].options, { runtimes: ['openclaw'], matcher: ['exec'] });
});

test('exec cap: a short result is left exactly as it is', async () => {
  const { run } = newCap();
  for (const n of [0, 11, 5000, MAX - 1, MAX]) {
    assert.equal(await run(execResult(output(n), execDetails({ exitCode: 3, status: 'failed' }))), undefined, `${n} chars`);
  }
});

test('exec cap: a long result, notice included, stays within 12,000 characters and keeps its head and tail', async () => {
  const { run } = newCap();
  for (const n of [MAX + 1, 12500, 30000, 99000, 100000, 500000]) {
    const original = output(n);
    const out = await run(execResult(original, execDetails()));
    const shown = cappedText(out);
    assert.ok(shown.length <= MAX, `${n} chars became ${shown.length}`);
    assert.ok(shown.length > 6000, 'most of the budget is used');
    assert.ok(shown.includes(original.slice(0, 500)), 'head kept');
    assert.ok(shown.includes(original.slice(-300)), 'tail kept');
    assert.match(shown, new RegExp(`${n} chars`));
  }
});

test('exec cap: an error line in the omitted middle is carried in the notice', async () => {
  const { run } = newCap();
  const original = output(60000, { marker: 'ERROR build step 7 failed: missing symbol frobnicate' });
  const shown = cappedText(await run(execResult(original, execDetails())));
  assert.ok(shown.length <= MAX);
  assert.ok(shown.includes('ERROR build step 7 failed: missing symbol frobnicate'));
  // Many error lines and very long ones still keep the whole result within the limit.
  const noisy = Array.from({ length: 400 }, (_, i) => `Traceback error ${i} ${'y'.repeat(500)}`).join('\n');
  const noisyShown = cappedText(await run(execResult(noisy, execDetails({ exitCode: 1, status: 'failed' }))));
  assert.ok(noisyShown.length <= MAX);
  assert.match(noisyShown, /exit code 1/);
});

test('exec cap: the exit status survives, from details, from the final exit line, or is marked unavailable', async () => {
  const { run } = newCap();
  const fromDetails = cappedText(await run(execResult(output(40000), execDetails({ exitCode: 3, status: 'failed', exitReason: 'exit' }))));
  assert.match(fromDetails, /exit code 3/);
  assert.match(fromDetails, /failed/);
  assert.match(cappedText(await run(execResult(output(40000), execDetails({ exitCode: 0 })))), /exit code 0/);

  const exitLine = `${output(40000)}\n\n(Command exited with code 7)`;
  const fromText = cappedText(await run(execResult(exitLine, { aggregated: exitLine })));
  assert.match(fromText, /exit code 7/);
  assert.ok(fromText.includes('(Command exited with code 7)'), 'the exit line itself stays in the tail');

  const unknown = cappedText(await run(execResult(output(40000), { aggregated: 'x' })));
  assert.match(unknown, /exit status: unavailable/i);
});

test('exec cap: a result OpenClaw already truncated is marked partial, and its status is unavailable unless present', async () => {
  const { run, dir } = newCap();
  const prefix = output(100000);
  const shown = cappedText(await run(execResult(prefix, { truncated: true, originalSizeBytes: 149114 })));
  assert.ok(shown.length <= MAX);
  assert.match(shown, /PARTIAL/);
  assert.match(shown, /OpenClaw/);
  assert.match(shown, /exit status: unavailable/i);
  assert.doesNotMatch(shown, /originalSizeBytes|149114/, 'that number is not the raw size, so it is not reported as one');
  assert.doesNotMatch(shown, /full (raw )?output/i, 'the saved prefix is never called the full output');
  const [file] = fs.readdirSync(dir);
  assert.match(file, /partial/i, 'the saved file is marked partial too');
  assert.equal(fs.readFileSync(path.join(dir, file), 'utf8'), prefix);

  // truncated:true with the exit line still at the end of the text: the status is visible, the result still partial.
  const withLine = `${output(99000)}\n(Command exited with code 3)`;
  const visible = cappedText(await run(execResult(withLine, { truncated: true, originalSizeBytes: 101048 })));
  assert.match(visible, /PARTIAL/);
  assert.match(visible, /exit code 3/);

  // A text at the 100,000-character ceiling is partial even when details say nothing.
  const atCeiling = cappedText(await run(execResult(output(100000), execDetails())));
  assert.match(atCeiling, /PARTIAL/);
  // An ordinary long result is not.
  assert.doesNotMatch(cappedText(await run(execResult(output(50000), execDetails()))), /PARTIAL/);
});

test('exec cap: the complete middleware-visible text is saved in a 0700 directory as a 0600 file the notice names', async () => {
  const { run, dir } = newCap();
  const original = output(45000);
  const shown = cappedText(await run(execResult(original, execDetails())));
  const files = fs.readdirSync(dir);
  assert.equal(files.length, 1);
  const file = path.join(dir, files[0]);
  assert.equal(fs.readFileSync(file, 'utf8'), original);
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  assert.equal(fs.statSync(dir).mode & 0o777, 0o700);
  assert.equal(fs.statSync(file).uid, process.getuid());
  assert.equal(fs.statSync(dir).uid, process.getuid());
  assert.ok(shown.includes(file), 'the notice gives the path');
  assert.match(shown, /head|tail|grep/, 'the notice says how to read it');
  assert.ok(!files[0].includes(TASK_UUID) && !/rmp_|task/.test(files[0]), 'the file name leaks no session or task id');
  // Two results never share a file.
  await run(execResult(original, execDetails()));
  assert.equal(fs.readdirSync(dir).length, 2);
});

test('exec cap: an existing directory with loose permissions is tightened; a symlinked directory is refused', async () => {
  const loose = newCap();
  fs.mkdirSync(loose.dir, { mode: 0o755 });
  fs.chmodSync(loose.dir, 0o755);
  await loose.run(execResult(output(30000), execDetails()));
  assert.equal(fs.statSync(loose.dir).mode & 0o777, 0o700);
  assert.equal(fs.readdirSync(loose.dir).length, 1);

  const base = tmpDir();
  const target = path.join(base, 'elsewhere');
  fs.mkdirSync(target);
  const link = path.join(base, 'link');
  fs.symlinkSync(target, link);
  const linked = newCap({ dir: link });
  const shown = cappedText(await linked.run(execResult(output(30000), execDetails())));
  assert.ok(shown.length <= MAX);
  assert.match(shown, /NOT saved/);
  assert.deepEqual(fs.readdirSync(target), [], 'nothing was written through the link');
});

test('exec cap: a file name that already exists, even as a symlink, is never overwritten or followed', async () => {
  const base = tmpDir();
  const dir = path.join(base, 'exec-results');
  fs.mkdirSync(dir, { mode: 0o700 });
  const victim = path.join(base, 'victim.txt');
  fs.writeFileSync(victim, 'precious');
  const { createExecResultCap } = capModule();
  const handler = createExecResultCap({ dir, ownerUid: process.geteuid(), randomHex: () => '0123456789abcdef', now: () => 1_700_000_000_000 });
  // Whatever name the module picks with this fixed randomness, plant a symlink on it and on its partial twin.
  const probe = tmpDir();
  const dry = createExecResultCap({ dir: path.join(probe, 'd'), ownerUid: process.geteuid(), randomHex: () => '0123456789abcdef', now: () => 1_700_000_000_000 });
  await dry({ toolName: 'exec', result: execResult(output(30000), execDetails()) }, { sessionKey: TASK_KEY });
  const [name] = fs.readdirSync(path.join(probe, 'd'));
  fs.symlinkSync(victim, path.join(dir, name));
  const out = await handler({ toolName: 'exec', result: execResult(output(30000), execDetails()) }, { sessionKey: TASK_KEY });
  assert.ok(cappedText(out).length <= MAX);
  assert.match(cappedText(out), /NOT saved/);
  assert.equal(fs.readFileSync(victim, 'utf8'), 'precious');
});

test('exec cap: saved files older than three days are removed, nothing else is', async () => {
  const base = tmpDir();
  const dir = path.join(base, 'exec-results');
  const t0 = Date.now();
  let clock = t0;
  const { createExecResultCap, RETENTION_MS } = capModule();
  assert.equal(RETENTION_MS, 3 * DAY_MS);
  const handler = createExecResultCap({ dir, ownerUid: process.geteuid(), now: () => clock });
  const run = () => handler({ toolName: 'exec', result: execResult(output(30000), execDetails()) }, { sessionKey: TASK_KEY });
  await run();
  const [oldName] = fs.readdirSync(dir);
  fs.utimesSync(path.join(dir, oldName), (t0 - 4 * DAY_MS) / 1000, (t0 - 4 * DAY_MS) / 1000);
  const young = `exec-${t0 - 2 * DAY_MS}-00000000deadbeef.txt`;
  fs.writeFileSync(path.join(dir, young), 'young');
  fs.utimesSync(path.join(dir, young), (t0 - 2 * DAY_MS) / 1000, (t0 - 2 * DAY_MS) / 1000);
  const stray = 'notes-not-ours.txt';
  fs.writeFileSync(path.join(dir, stray), 'stray');
  fs.utimesSync(path.join(dir, stray), (t0 - 9 * DAY_MS) / 1000, (t0 - 9 * DAY_MS) / 1000);
  const keepTarget = path.join(base, 'keep.txt');
  fs.writeFileSync(keepTarget, 'keep');
  const oldLink = `exec-${t0 - 9 * DAY_MS}-00000000cafebabe.txt`;
  fs.symlinkSync(keepTarget, path.join(dir, oldLink));
  fs.mkdirSync(path.join(dir, `exec-${t0 - 9 * DAY_MS}-00000000feedface.txt`));
  clock = t0 + 11 * 60 * 1000; // past the throttle between sweeps
  await run();
  const left = fs.readdirSync(dir);
  assert.ok(!left.includes(oldName), 'the 4-day-old file is gone');
  assert.ok(left.includes(young), 'the 2-day-old file stays');
  assert.ok(left.includes(stray), 'a file this module did not write stays');
  assert.ok(left.includes(oldLink), 'a symlink is never followed or removed as a result file');
  assert.equal(fs.readFileSync(keepTarget, 'utf8'), 'keep');
  assert.equal(left.filter((n) => n.startsWith('exec-')).length, 4, 'the young file, the symlink, the directory and the new result');
});

test('exec cap: a failure to save still returns a capped result with a clear notice, and cleanup trouble is harmless', async () => {
  const base = tmpDir();
  const blocker = path.join(base, 'a-file');
  fs.writeFileSync(blocker, 'x');
  const broken = newCap({ dir: path.join(blocker, 'exec-results') });
  const shown = cappedText(await broken.run(execResult(output(50000), execDetails({ exitCode: 3, status: 'failed' }))));
  assert.ok(shown.length <= MAX);
  assert.match(shown, /NOT saved/);
  assert.match(shown, /exit code 3/);
  assert.doesNotMatch(shown, new RegExp(blocker.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')), 'no phantom path is offered');
});

test('exec cap: the handler never throws; if building the notice fails it falls back to a plain head and tail', async () => {
  const { run } = newCap();
  const hostile = { content: [{ type: 'text', text: output(60000) }] };
  Object.defineProperty(hostile, 'details', { get() { throw new Error('details exploded'); } });
  const fallback = cappedText(await run(hostile));
  assert.ok(fallback.length <= MAX);
  assert.ok(fallback.includes(output(60000).slice(0, 200)));
  assert.match(fallback, /RMP output cap/);
  for (const bad of [undefined, null, {}, { content: null }, { content: 'a string' }, { content: [{ type: 'image' }] }, { content: [null] }]) {
    await assert.doesNotReject(async () => run(bad));
    assert.equal(await run(bad), undefined);
  }
  const { createExecResultCap } = capModule();
  const handler = createExecResultCap({ dir: path.join(tmpDir(), 'd'), ownerUid: process.geteuid() });
  await assert.doesNotReject(async () => handler(undefined, undefined));
  await assert.doesNotReject(async () => handler({ toolName: 'exec' }, null));
});

test('exec cap: only exec in a task session is capped, retries and recall sessions included', async () => {
  const { run } = newCap();
  const long = () => execResult(output(40000), execDetails());
  for (const key of [TASK_KEY, `${TASK_KEY}__r1`, `${TASK_KEY}__r12`, `${TASK_KEY}__recall`, `agent:other:rmp_task_${TASK_UUID}`]) {
    assert.ok(cappedText(await run(long(), key)).length <= MAX, key);
  }
  for (const key of [
    `agent:main:rmp_verify_${TASK_UUID}`, 'agent:main:main', 'agent:main:slack:channel:dtest', 'agent:main:spike_plain_1', '',
    null, `agent:main:rmp_task_${TASK_UUID}:sub`, `agent:main:rmp_task_${TASK_UUID}__`, 'agent:main:rmp_task_not-a-uuid',
    `x:agent:main:rmp_task_${TASK_UUID}`, `agent:main:rmp_task_${TASK_UUID}\nagent:main:main`,
  ]) {
    assert.equal(await run(long(), key), undefined, `${key}`);
  }
  assert.equal(await run(long(), TASK_KEY, 'read'), undefined);
  assert.equal(await run(long(), TASK_KEY, 'web_fetch'), undefined);
});

test('exec cap: content and details keep the shape OpenClaw expects', async () => {
  const { run } = newCap();
  const image = { type: 'image', data: 'AAAA', mimeType: 'image/png' };
  const original = output(40000);
  const result = { content: [{ type: 'text', text: original.slice(0, 20000) }, image, { type: 'text', text: original.slice(20000) }], details: execDetails({ aggregated: original }), isError: false, extra: { kept: true } };
  const out = await run(result);
  const capped = out.result;
  assert.equal(capped.content[0].type, 'text');
  assert.ok(capped.content[0].text.length <= MAX);
  assert.deepEqual(capped.content.slice(1), [image], 'non-text blocks are carried over');
  assert.equal(capped.details.aggregated, capped.content[0].text, 'aggregated agrees with what the model sees');
  assert.equal(capped.details.exitCode, 0);
  assert.equal(capped.details.status, 'completed');
  assert.deepEqual(capped.extra, { kept: true });
  assert.equal(capped.isError, false);
  assert.deepEqual(Object.keys(capped.details).sort(), Object.keys(result.details).sort(), 'no key is added or lost');
  assert.equal(result.content.length, 3, 'the input object is not mutated');
  assert.equal(result.details.aggregated, original);

  const noAgg = (await run(execResult(original, { truncated: true, originalSizeBytes: 5 }))).result;
  assert.deepEqual(Object.keys(noAgg.details).sort(), ['originalSizeBytes', 'truncated']);
  const noDetails = (await run(execResult(original))).result;
  assert.equal(noDetails.details, undefined);
});

test('exec cap: the plugin module spawns no process and the repo files stay readable by the live-copy check', () => {
  const src = realReadFileSync(CAP_MODULE, 'utf8');
  assert.doesNotMatch(src, /child_process|execFileSync|execSync|spawnSync/);
  assert.doesNotMatch(src, /openclaw\.json|settings\.json|process\.env/, 'it reads no config or environment');
});

test('exec cap: the default directory is private under RMP state, and the limits are the documented ones', () => {
  const { DEFAULT_DIR, MAX_RESULT_CHARS, UPSTREAM_LIMIT_CHARS } = capModule();
  assert.equal(DEFAULT_DIR, '/root/.openclaw/rmp/exec-results');
  assert.equal(MAX_RESULT_CHARS, 12000);
  assert.equal(UPSTREAM_LIMIT_CHARS, 100000);
});

// ---- exec cap, release blockers: hard cap on every path, root ownership, error summary, full/partial truth ----

function shownLength(out) {
  return out.result.content.filter((b) => b && b.type === 'text').reduce((n, b) => n + b.text.length, 0);
}

function lonelySurrogate(textValue) {
  return /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(textValue);
}

test('exec cap: a long exec never leaves the handler above 12,000 characters, whichever step fails', async () => {
  const long = output(150000);
  const boom = () => { throw new Error('injected'); };
  const faults = {
    'content getter throws': () => { const r = {}; Object.defineProperty(r, 'content', { get: boom }); return { result: r }; },
    'event.result getter throws': () => { const e = { toolName: 'exec' }; Object.defineProperty(e, 'result', { get: boom }); return e; },
    'text read fails on the second access': () => {
      let reads = 0;
      const block = { type: 'text' };
      Object.defineProperty(block, 'text', { get() { if (++reads > 1) boom(); return long; } });
      return { result: { content: [block] } };
    },
    'details getter throws': () => { const r = { content: [{ type: 'text', text: long }] }; Object.defineProperty(r, 'details', { get: boom }); return { result: r }; },
    'result keys cannot be listed': () => ({ result: new Proxy({ content: [{ type: 'text', text: long }] }, { ownKeys: boom }) }),
    'result is a proxy that throws on every read': () => ({ result: new Proxy({}, { get: boom, ownKeys: boom, has: boom, getOwnPropertyDescriptor: boom }) }),
    'content is a long string, not blocks': () => ({ result: { content: long } }),
    'content blocks are a proxy array': () => ({ result: { content: new Proxy([{ type: 'text', text: long }], { get(t, k, r) { if (k === 'filter' || k === 'map') return boom; return Reflect.get(t, k, r); } }) } }),
  };
  const sessions = { sessionKey: TASK_KEY };
  const throwingLog = { log: boom };
  for (const [name, make] of Object.entries(faults)) {
    for (const opts of [{}, throwingLog]) {
      const { handler } = newCap({ opts });
      const event = make();
      const full = 'toolName' in event ? event : { toolName: 'exec', isError: false, ...event };
      const out = await handler(full, sessions);
      assert.ok(out && out.result, `${name}: a bounded result is returned, never the original`);
      assert.ok(shownLength(out) <= MAX, `${name}: ${shownLength(out)} chars`);
    }
  }
  // Collaborators that fail: the clock, the random source, the directory, the disk, the sweep.
  const collaborators = {
    'clock throws': { now: boom },
    'random source throws': { randomHex: boom },
    'directory is not a string': { dir: 42 },
    'directory is empty': { dir: '' },
    'log throws': { log: boom },
  };
  for (const [name, opts] of Object.entries(collaborators)) {
    const { handler } = newCap({ opts, dir: opts.dir });
    const out = await handler({ toolName: 'exec', result: execResult(long, execDetails({ exitCode: 2 })) }, sessions);
    assert.ok(shownLength(out) <= MAX, name);
  }
  for (const fn of ['writeSync', 'fstatSync', 'fchmodSync', 'openSync', 'mkdirSync', 'readdirSync', 'lstatSync']) {
    const injected = mock.method(fs, fn, boom);
    try {
      const { handler } = newCap();
      const out = await handler({ toolName: 'exec', result: execResult(long, execDetails()) }, sessions);
      assert.ok(shownLength(out) <= MAX, `fs.${fn} throws`);
    } finally {
      injected.mock.restore();
    }
  }
});

test('exec cap: whatever the output looks like, the result stays within 12,000 characters', async () => {
  const { run } = newCap();
  let seed = 12345;
  const rand = (n) => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed % n; };
  const pieces = ['x', 'é', '😀', '\n', '\r\n', 'error: boom\n', 'Traceback (most recent call last)\n', ' ', '\u0000', 'a'.repeat(900), '\n'.repeat(50)];
  for (let i = 0; i < 60; i++) {
    let body = '';
    const target = MAX + 1 + rand(180000);
    while (body.length < target) body += pieces[rand(pieces.length)];
    const details = [execDetails({ exitCode: rand(3) }), { truncated: true, originalSizeBytes: 1 }, undefined, { aggregated: body }][rand(4)];
    const out = await run(execResult(body, details));
    assert.ok(shownLength(out) <= MAX, `case ${i}: ${body.length} chars became ${shownLength(out)}`);
    assert.ok(!lonelySurrogate(out.result.content[0].text), `case ${i}: a surrogate pair was cut in two`);
  }
});

test('exec cap: only a result that is already short is ever returned unchanged', async () => {
  const { run } = newCap();
  for (const result of [execResult(output(MAX)), { content: [{ type: 'text', text: 'a' }, { type: 'text', text: 'b' }] }, { content: [{ type: 'image', data: 'AAAA' }] }, { content: 'short' }, { content: [] }]) {
    assert.equal(await run(result), undefined);
  }
  // Two blocks that are each short but together over the limit are capped.
  const two = { content: [{ type: 'text', text: output(7000) }, { type: 'text', text: output(7000) }] };
  assert.ok(shownLength(await run(two)) <= MAX);
});

test('exec cap: files and the directory belong to root, and a tree that would not is never created', async () => {
  const { createExecResultCap, DEFAULT_OWNER_UID } = capModule();
  assert.equal(DEFAULT_OWNER_UID, 0, 'root-only means uid 0');

  // With no owner given, the module requires root: as root it saves root-owned files, as anyone else it saves nothing.
  const base = tmpDir();
  const dir = path.join(base, 'exec-results');
  const strict = createExecResultCap({ dir });
  const shown = cappedText(await strict({ toolName: 'exec', result: execResult(output(40000), execDetails()) }, { sessionKey: TASK_KEY }));
  assert.ok(shown.length <= MAX);
  if (process.geteuid() === 0) {
    const [file] = fs.readdirSync(dir);
    assert.equal(fs.statSync(path.join(dir, file)).uid, 0);
    assert.equal(fs.statSync(dir).uid, 0);
    assert.equal(fs.statSync(path.join(dir, file)).mode & 0o777, 0o600);
    assert.equal(fs.statSync(dir).mode & 0o777, 0o700);
  } else {
    assert.match(shown, /NOT saved/);
    assert.equal(fs.existsSync(dir), false, 'a non-root process creates no tree that only claims to be root-only');
  }

  // A required owner that is not the running user: refused before anything is created.
  const elsewhere = path.join(tmpDir(), 'exec-results');
  const wrongOwner = createExecResultCap({ dir: elsewhere, ownerUid: process.geteuid() + 1 });
  const refused = cappedText(await wrongOwner({ toolName: 'exec', result: execResult(output(40000), execDetails()) }, { sessionKey: TASK_KEY }));
  assert.match(refused, /NOT saved/);
  assert.ok(refused.length <= MAX);
  assert.equal(fs.existsSync(elsewhere), false);
});

test('exec cap: an existing directory or file owned by another user is refused', { skip: process.geteuid() !== 0 && 'needs root to chown' }, async () => {
  const base = tmpDir();
  const dir = path.join(base, 'exec-results');
  fs.mkdirSync(dir, { mode: 0o700 });
  fs.chownSync(dir, 4242, 4242);
  const { run } = newCap({ dir, opts: { ownerUid: 0 } });
  const shown = cappedText(await run(execResult(output(40000), execDetails())));
  assert.match(shown, /NOT saved/);
  assert.deepEqual(fs.readdirSync(dir), []);
});

test('exec cap: many error lines are counted and sampled, the notice stays in bounds and keeps the exit status', async () => {
  const { run } = newCap();
  // Twenty different errors in the omitted middle, and a long text around them.
  const distinct = Array.from({ length: 20 }, (_, i) => `ERROR module${String.fromCharCode(97 + i)} failed with reason ${'r'.repeat(i + 1)}`);
  const body = `${output(30000)}\n${distinct.join('\n')}\n${output(30000)}\n(Command exited with code 4)`;
  const shown = cappedText(await run(execResult(body, execDetails({ exitCode: 4, status: 'failed' }))));
  assert.ok(shown.length <= MAX);
  assert.match(shown, /exit code 4/);
  assert.match(shown, /Error-like lines in the omitted part: 20 matching, 20 distinct, showing 6/);
  assert.ok(shown.includes(distinct[0]) && shown.includes(distinct[3]), 'the first four are listed');
  assert.ok(shown.includes(distinct[18]) && shown.includes(distinct[19]), 'the last two are listed');
  assert.ok(!shown.includes(distinct[10]), 'the rest are only counted');
  assert.ok(shown.includes('(Command exited with code 4)'));

  // Hundreds of lines that differ only in a number are one pattern, so a different error among them still shows.
  const noisy = Array.from({ length: 600 }, (_, i) => `Traceback error code ${i} while retrying`);
  noisy.splice(300, 0, 'FATAL: disk quota exceeded on /var/lib');
  const wide = `${output(20000)}\n${noisy.join('\n')}\n${output(20000)}`;
  const second = cappedText(await run(execResult(wide, execDetails({ exitCode: 1, status: 'failed' }))));
  assert.ok(second.length <= MAX);
  assert.match(second, /601 matching, 2 distinct, showing 2/);
  assert.ok(second.includes('FATAL: disk quota exceeded on /var/lib'));
  assert.ok(second.includes('Traceback error code 0 while retrying'));

  // Very long error lines are shortened, and up to six of them still fit.
  const huge = Array.from({ length: 40 }, (_, i) => `error ${String.fromCharCode(65 + (i % 26))}${i} ${'z'.repeat(5000)}`).join('\n');
  const third = cappedText(await run(execResult(`${output(20000)}\n${huge}\n${output(20000)}`, execDetails({ exitCode: 2 }))));
  assert.ok(third.length <= MAX);
  assert.match(third, /exit code 2/);
});

test('exec cap: error lines and output are never written to the log, only error codes', async () => {
  const lines = [];
  const secret = 'SECRETTOKEN-abc123';
  const body = `${output(20000)}\nerror: leaked ${secret}\n${output(20000)}`;
  const blocker = path.join(tmpDir(), 'a-file');
  fs.writeFileSync(blocker, 'x');
  const { run } = newCap({ dir: path.join(blocker, 'exec-results'), opts: { log: (m) => lines.push(String(m)) } });
  await run(execResult(body, execDetails()));
  const hostile = { content: [{ type: 'text', text: body }] };
  Object.defineProperty(hostile, 'details', { get() { throw new Error(`explode ${secret}`); } });
  await run(hostile);
  assert.ok(lines.length > 0, 'failures are logged');
  assert.ok(lines.every((m) => !m.includes(secret) && m.length < 200), lines.join('|'));
});

test('exec cap: the saved file is exactly what the middleware saw, and partial is marked everywhere or nowhere', async () => {
  // Several text blocks are saved as the model would read them: joined by a newline. Non-text blocks are not text.
  const base = tmpDir();
  const dir = path.join(base, 'exec-results');
  const { run } = newCap({ dir });
  const a = `${output(9000)}é😀`;
  const b = output(9000);
  await run({ content: [{ type: 'text', text: a }, { type: 'image', data: 'AAAA' }, { type: 'text', text: b }], details: execDetails() });
  const [file] = fs.readdirSync(dir);
  const bytes = fs.readFileSync(path.join(dir, file));
  assert.equal(bytes.toString('utf8'), `${a}\n${b}`);
  assert.equal(fs.statSync(path.join(dir, file)).size, Buffer.byteLength(`${a}\n${b}`));
  assert.doesNotMatch(file, /partial/);

  // Not partial: no partial wording anywhere, and the file says complete.
  const plain = cappedText(await run(execResult(output(50000), execDetails())));
  assert.doesNotMatch(plain, /PARTIAL|partial|truncat/i);
  assert.match(plain, /Saved the complete output/);

  // Partial, by the flag or by the length: the file name, the notice and the wording agree, even when saving fails.
  for (const [textValue, details] of [[output(99000), { truncated: true, originalSizeBytes: 101048 }], [output(100000), execDetails()], [output(100000), { truncated: true, originalSizeBytes: 149114 }]]) {
    const before = fs.readdirSync(dir).length;
    const shown = cappedText(await run(execResult(textValue, details)));
    const names = fs.readdirSync(dir);
    assert.equal(names.length, before + 1);
    const partialFile = names.find((n) => /\.partial\.txt$/.test(n) && fs.readFileSync(path.join(dir, n), 'utf8') === textValue);
    assert.ok(partialFile, 'a .partial file holds exactly the visible text');
    assert.match(shown, /PARTIAL/);
    assert.match(shown, /100000/, 'the upstream limit is stated');
    assert.match(shown, /NOT the complete raw output/);
    assert.doesNotMatch(shown, /Saved the complete output|full (raw )?output/i);
  }
  const blocker = path.join(tmpDir(), 'a-file');
  fs.writeFileSync(blocker, 'x');
  const broken = newCap({ dir: path.join(blocker, 'x') });
  const failed = cappedText(await broken.run(execResult(output(100000), { truncated: true, originalSizeBytes: 149114 })));
  assert.match(failed, /PARTIAL/);
  assert.match(failed, /NOT saved/);
  assert.match(failed, /exit status: unavailable/i);
});
