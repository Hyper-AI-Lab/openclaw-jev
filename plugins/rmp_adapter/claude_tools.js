'use strict';

// Aura's direct Claude tools. RMP runs each turn of Claude Code in its own unit (app/coding/direct.py);
// these tools only call RMP's API, so a turn outlives an RMP restart and a stop reaches it.

const POLL_SEC = 50;
const REPLY_CHARS = 30000;
// RMP may be restarting (its code reloads); the turn runs on, so the wait does too, this long.
const OUTAGE_SEC = 300;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function format(state) {
  const lines = [state.done ? `Claude, turn ${state.turn}: ${state.outcome}` : `Claude is still working on turn ${state.turn}.`];
  if (state.done) {
    const reply = state.reply || '(no reply)';
    lines.push('', reply.length > REPLY_CHARS
      ? `${reply.slice(0, REPLY_CHARS)}\n[cut at ${REPLY_CHARS} characters; the whole answer is in the session's record]`
      : reply);
  } else if (state.progress && state.progress.length) {
    lines.push(`Latest: ${state.progress.slice(-5).join('; ')}`, 'Wait for it with claude_status, or stop it with claude_end.');
  }
  if (state.files_edited && state.files_edited.length) lines.push('', `Files edited: ${state.files_edited.join(', ')}`);
  if (state.commands && state.commands.length) {
    lines.push(`Commands (last ${state.commands.length}): ${state.commands.map((c) => c.split('\n')[0]).join(' | ')}`);
  }
  if (state.denied && state.denied.length) lines.push(`Blocked by auto mode: ${state.denied.join(', ')}`);
  if (state.outcome === 'usage_limit' && state.resets_at) {
    lines.push(`Claude's usage limit is reached; it resets at ${new Date(state.resets_at * 1000).toISOString()}.`);
  } else if (state.done && state.outcome !== 'success' && state.error) {
    lines.push(`Error: ${state.error}`);
  }
  return lines.join('\n');
}

function register(api, { rmpFetch, pause = sleep }) {
  const turnPath = (session, turn) => `/api/claude/sessions/${encodeURIComponent(session)}/turns/${Number(turn)}`;

  async function waitFor(session, turn, waitSec, signal) {
    const until = waitSec ? Date.now() + waitSec * 1000 : Infinity;
    let failingSince = null;
    for (;;) {
      const left = Math.max(1, Math.min(POLL_SEC, Math.ceil((until - Date.now()) / 1000)));
      let state;
      try {
        state = await rmpFetch('GET', `${turnPath(session, turn)}?wait=${left}`, undefined, { maxTimeSec: left + 20 });
        failingSince = null;
      } catch (err) {
        failingSince = failingSince || Date.now();
        if (/^HTTP 4\d\d/.test(err.message) || signal?.aborted || Date.now() - failingSince > OUTAGE_SEC * 1000) throw err;
        await pause(5000);
        continue;
      }
      if (state.done || signal?.aborted || Date.now() >= until) return format(state);
    }
  }

  api.registerTool((context) => ({
    name: 'claude_start',
    description:
      'Start a conversation with Claude Code, a strong coding agent, for this task. RMP runs it as root on this server in '
      + "Claude's auto permission mode, in its own workspace: \"repo\" is a fresh clone of your repository "
      + '(Hyper-AI-Lab/openclaw-jev) from GitHub, "scratch" an empty folder. Use it whenever it helps: questions about code, '
      + 'analysis, scripts, data work, changes to your code. Then talk to it with claude_send, and end with claude_end.',
    parameters: {
      type: 'object',
      properties: {
        workspace: { type: 'string', enum: ['repo', 'scratch'], description: 'repo (default) or scratch' },
        title: { type: 'string', description: 'What the session is for, in a few words' },
      },
    },
    execute: async (_id, params) => {
      try {
        const s = await rmpFetch('POST', '/api/claude/sessions', {
          session_key: context?.sessionKey || '',
          workspace: params.workspace || 'repo',
          title: params.title || '',
        }, { maxTimeSec: 330 });
        return `Claude session ${s.id} is ready (${s.workspace}: ${s.path}). Send it a message with claude_send.`;
      } catch (err) {
        return `Claude session not started: ${err.message}`;
      }
    },
  }), { name: 'claude_start' });

  api.registerTool({
    name: 'claude_send',
    description:
      'Send Claude a message in a session and wait for its answer. One message is one turn: Claude reads, edits and runs '
      + 'commands until it answers, which can take minutes; your reply deadline stays open meanwhile. Write it like a brief '
      + 'to a senior engineer: the goal, the context, the constraints, and what to report. Returns its answer, the files it '
      + 'edited and the commands it ran.',
    parameters: {
      type: 'object',
      properties: {
        session: { type: 'string', description: 'The session id from claude_start' },
        message: { type: 'string' },
        wait_minutes: { type: 'number', description: 'Stop waiting after this long (default: until it answers)' },
      },
      required: ['session', 'message'],
    },
    execute: async (_id, params, signal) => {
      try {
        const started = await rmpFetch('POST', `/api/claude/sessions/${encodeURIComponent(params.session)}/messages`,
          { message: params.message }, { maxTimeSec: 60 });
        return await waitFor(params.session, started.turn, params.wait_minutes ? params.wait_minutes * 60 : 0, signal);
      } catch (err) {
        return `Claude message failed: ${err.message}`;
      }
    },
  });

  api.registerTool({
    name: 'claude_status',
    description:
      "Where a Claude session stands: its latest turn's answer, or its progress while it works. With wait_seconds (up to 55) "
      + 'it first waits that long for the turn to finish.',
    parameters: {
      type: 'object',
      properties: {
        session: { type: 'string' },
        wait_seconds: { type: 'number' },
      },
      required: ['session'],
    },
    execute: async (_id, params, signal) => {
      try {
        const s = await rmpFetch('GET', `/api/claude/sessions/${encodeURIComponent(params.session)}`);
        if (!s.turns) return `Claude session ${s.id} (${s.status}) has no turns yet.`;
        return await waitFor(s.id, s.turns, Math.min(Number(params.wait_seconds) || 1, 55), signal);
      } catch (err) {
        return `Claude status failed: ${err.message}`;
      }
    },
  });

  api.registerTool((context) => ({
    name: 'deploy_pr',
    description:
      'Merge one of your pull requests on Hyper-AI-Lab/openclaw-jev and deploy it. RMP merges it once CI\'s test check has '
      + 'passed, then deploys GitHub\'s main when you are idle (restarting what changed, checking health, readiness and the '
      + 'canary, and reverting on failure) and sends Kirill a note with the link. Only for your own repository.',
    parameters: {
      type: 'object',
      properties: { pr: { type: 'number', description: 'The pull request number' } },
      required: ['pr'],
    },
    execute: async (_id, params) => {
      try {
        const r = await rmpFetch('POST', '/api/claude/deploy',
          { pr: Number(params.pr), session_key: context?.sessionKey || '' }, { maxTimeSec: 120 });
        return r.summary;
      } catch (err) {
        return `deploy_pr failed: ${err.message}`;
      }
    },
  }), { name: 'deploy_pr' });

  api.registerTool({
    name: 'claude_end',
    description: "End a Claude session: a turn still running is stopped. The session's record stays in RMP's memory.",
    parameters: {
      type: 'object',
      properties: { session: { type: 'string' } },
      required: ['session'],
    },
    execute: async (_id, params) => {
      try {
        const s = await rmpFetch('POST', `/api/claude/sessions/${encodeURIComponent(params.session)}/end`, undefined,
          { maxTimeSec: 60 });
        return `Claude session ${s.id} ended after ${s.turns} turn(s)${s.stopped.length ? '; its running turn was stopped' : ''}.`;
      } catch (err) {
        return `Claude session not ended: ${err.message}`;
      }
    },
  });
}

module.exports = { register, format };
