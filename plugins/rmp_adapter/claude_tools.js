'use strict';

// Aura's direct Claude tools. RMP runs each turn of Claude Code in its own unit (app/coding/direct.py);
// these tools only call RMP's API, so a turn outlives an RMP restart and a stop reaches it. attach_file
// sends Kirill a file Claude made, with her accepted reply (app/coding/outbox.py).

const POLL_SEC = 50;
// Aura's whole context is bounded; the full answer stays in the session's record.
const REPLY_CHARS = 12000;
// RMP may be restarting (its code reloads); the turn runs on, so the wait does too, this long.
const OUTAGE_SEC = 300;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// OpenClaw's tool result: the text Aura reads, and a few facts a script can use.
function result(text, details) {
  return { content: [{ type: 'text', text }], details: details || {} };
}

function format(state) {
  const kind = state.plan ? ' (planning)' : '';
  const lines = [state.done ? `Claude, turn ${state.turn}${kind}: ${state.outcome}` : `Claude is still working on turn ${state.turn}${kind}.`];
  if (state.done) {
    const reply = state.reply || '(no reply)';
    lines.push('', reply.length > REPLY_CHARS
      ? `${reply.slice(0, REPLY_CHARS)}\n[cut at ${REPLY_CHARS} characters; the whole answer is in the session's record]`
      : reply);
  } else {
    // Asked often while Claude works, so kept short; the files and commands come with the answer.
    if (state.progress && state.progress.length) lines.push(`Latest: ${state.progress.slice(-3).join('; ')}`);
    lines.push('Wait for it with claude_status, or stop it with claude_end.');
    return lines.join('\n');
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

function turnResult(state) {
  return result(format(state), { session: state.session, turn: state.turn, done: state.done, outcome: state.outcome });
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
      if (state.done || signal?.aborted || Date.now() >= until) return turnResult(state);
    }
  }

  api.registerTool((context) => ({
    name: 'claude_start',
    description:
      'Start a conversation with Claude Code, a strong coding agent, for this task. RMP runs it as root on this server, in '
      + 'its own workspace: "repo" is a fresh clone of your repository (Hyper-AI-Lab/openclaw-jev) from GitHub, for work on '
      + 'your own code; "scratch" is an empty folder, for everything else, such as a script, a file conversion, data work '
      + 'or an analysis, with no pull request. Use it whenever it helps. Then talk to it with claude_send, and end with '
      + 'claude_end.',
    parameters: {
      type: 'object',
      properties: {
        workspace: {
          type: 'string',
          enum: ['repo', 'scratch'],
          description: 'repo (default) for work on your own code; scratch for everything else',
        },
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
        return result(`Claude session ${s.id} is ready (${s.workspace}: ${s.path}). Send it a message with claude_send.`,
          { session: s.id, workspace: s.workspace, path: s.path });
      } catch (err) {
        return result(`Claude session not started: ${err.message}`, { error: err.message });
      }
    },
  }), { name: 'claude_start' });

  api.registerTool({
    name: 'claude_send',
    description:
      'Send Claude a message in a session and wait for its answer. One message is one turn: Claude reads, edits and runs '
      + 'commands until it answers, which can take minutes; your reply deadline stays open meanwhile. Write it like a brief '
      + 'to a senior engineer: the goal, the context, the constraints, and what to report. Returns its answer, the files it '
      + 'edited and the commands it ran. Claude plans on Opus and carries out on Sonnet: with plan true it only investigates '
      + 'and answers with a plan, changing nothing; review it, then send the go-ahead without plan.',
    parameters: {
      type: 'object',
      properties: {
        session: { type: 'string', description: 'The session id from claude_start' },
        message: { type: 'string' },
        plan: { type: 'boolean', description: 'A planning turn on Opus: for a change or anything non-trivial, before the work' },
        effort: {
          type: 'string',
          enum: ['medium', 'high', 'xhigh'],
          description: 'How hard Claude thinks: medium (default) for clear-scope work, high for bug fixes, xhigh for hard investigations',
        },
        wait_minutes: { type: 'number', description: 'Stop waiting after this long (default: until it answers)' },
      },
      required: ['session', 'message'],
    },
    execute: async (_id, params, signal) => {
      try {
        const started = await rmpFetch('POST', `/api/claude/sessions/${encodeURIComponent(params.session)}/messages`,
          { message: params.message, plan: Boolean(params.plan), effort: params.effort }, { maxTimeSec: 60 });
        return await waitFor(params.session, started.turn, params.wait_minutes ? params.wait_minutes * 60 : 0, signal);
      } catch (err) {
        return result(`Claude message failed: ${err.message}`, { error: err.message });
      }
    },
  });

  api.registerTool({
    name: 'claude_status',
    description:
      "Where a Claude session stands: its latest turn's answer, or its progress while it works. It first waits up to "
      + 'wait_seconds (default and most 55) for the turn to finish.',
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
        if (!s.turns) return result(`Claude session ${s.id} (${s.status}) has no turns yet.`, { session: s.id, turn: 0 });
        return await waitFor(s.id, s.turns, Math.min(Number(params.wait_seconds) || 55, 55), signal);
      } catch (err) {
        return result(`Claude status failed: ${err.message}`, { error: err.message });
      }
    },
  });

  api.registerTool((context) => ({
    name: 'attach_file',
    description:
      "Attach a file to your reply to Kirill: RMP sends it in his Slack DM with your reply once the evaluator accepts the "
      + "reply. A file from one of this task's Claude sessions, or one you made in /root/.openclaw/media/outbound; up to "
      + '50 MB. RMP refuses one that looks like it holds a secret.',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'The full path of the file' },
        title: { type: 'string', description: 'What Kirill sees as its title (default: the file name)' },
      },
      required: ['path'],
    },
    execute: async (_id, params) => {
      try {
        const f = await rmpFetch('POST', '/api/replies/files',
          { session_key: context?.sessionKey || '', path: params.path, title: params.title || '' }, { maxTimeSec: 120 });
        return result(`Attached ${f.name} (${f.size} bytes): it goes to Kirill with your reply.`,
          { id: f.id, name: f.name, size: f.size });
      } catch (err) {
        return result(`File not attached: ${err.message}`, { error: err.message });
      }
    },
  }), { name: 'attach_file' });

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
        return result(`Claude session ${s.id} ended after ${s.turns} turn(s)${s.stopped.length ? '; its running turn was stopped' : ''}.`,
          { session: s.id, turns: s.turns });
      } catch (err) {
        return result(`Claude session not ended: ${err.message}`, { error: err.message });
      }
    },
  });
}

module.exports = { register, format };
