'use strict';

// Caps what an exec call in an RMP task session puts into the model's context.
//
// OpenClaw lets a plugin rewrite a tool result before the model sees it (agent tool result
// middleware). A result longer than MAX_RESULT_CHARS is replaced by a notice, the start and the end
// of the output; the text RMP saw is saved to a private file the model can search with head, tail and
// grep. Short results, other tools and other sessions are left exactly as they are.
//
// What RMP can see is not always the raw output: OpenClaw truncates a result to about 100,000
// characters before any plugin runs (and drops the exit line with it). Such a result is marked PARTIAL
// in both the notice and the saved file's name, and the saved text is never called the full output.

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const MAX_RESULT_CHARS = 12000;
// OpenClaw's own ceiling on the text a middleware receives.
const UPSTREAM_LIMIT_CHARS = 100000;
const RETENTION_MS = 3 * 24 * 3600 * 1000;
const SWEEP_INTERVAL_MS = 10 * 60 * 1000;
// A sibling of the RMP checkout, not inside it: runtime files must not dirty the code tree the deploy owns.
const DEFAULT_DIR = '/root/.openclaw/rmp-exec-results';
// "Root-only" means uid 0: the directory and every file must belong to it, and nothing is created by any other user.
const DEFAULT_OWNER_UID = 0;

// Room kept for the notice; head and tail share the rest, so the whole result never exceeds MAX_RESULT_CHARS.
const NOTICE_RESERVE = 2400;
const MAX_ERROR_LINES = 6;
const ERROR_LINE_CHARS = 160;

// Aura's task sessions, with their retries (__r<n>) and the recall refinement (__recall).
const TASK_SESSION = /^agent:[^:]+:rmp_task_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(__[a-z0-9]+)?$/i;
const RESULT_FILE = /^exec-(\d+)-[0-9a-f]{16}(\.partial)?\.txt$/;
const EXIT_LINE = /\(Command exited with code (\d+)\)\s*$/;
const ERROR_LINE = /\b(errors?|fatal|exception|traceback|failed|failure|panic|denied|segmentation fault|no such file|not found|cannot|can't|unable to|killed|timed out)\b/i;

function isTaskSession(key) {
  return typeof key === 'string' && TASK_SESSION.test(key);
}

/** The text of a result as the model would read it (text blocks joined by a newline), or null when it has none. */
function visibleText(result) {
  const content = result && result.content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return null;
  const blocks = content.filter((b) => b && b.type === 'text' && typeof b.text === 'string');
  return blocks.length ? blocks.map((b) => b.text).join('\n') : null;
}

function exitStatus(details, text) {
  const code = details && Number.isInteger(details.exitCode) ? details.exitCode : null;
  if (code !== null) {
    const extra = [];
    if (typeof details.status === 'string') extra.push(`status ${details.status}`);
    if (typeof details.exitSignal === 'string' && details.exitSignal) extra.push(`signal ${details.exitSignal}`);
    if (details.timedOut === true) extra.push('timed out');
    return `exit code ${code}${extra.length ? `, ${extra.join(', ')}` : ''}`;
  }
  const line = EXIT_LINE.exec(text.slice(-300));
  if (line) return `exit code ${line[1]} (from the final line of the output)`;
  return null;
}

/**
 * Error-looking lines of the part of the output the model will not see. A line matches ERROR_LINE;
 * lines that differ only in their digits count as one pattern. With more than six patterns the first
 * four and the last two are listed, and the notice says how many lines and patterns there were.
 */
function errorSummary(omitted, budget) {
  let matching = 0;
  const patterns = new Map();
  for (const raw of omitted.split('\n')) {
    if (!ERROR_LINE.test(raw)) continue;
    matching += 1;
    const line = raw.trim().slice(0, ERROR_LINE_CHARS);
    const key = line.replace(/\d+/g, '#');
    if (line && !patterns.has(key)) patterns.set(key, line);
  }
  if (!matching) return null;
  const distinct = [...patterns.values()];
  const picked = distinct.length <= MAX_ERROR_LINES ? distinct : [...distinct.slice(0, 4), ...distinct.slice(-2)];
  const shown = [];
  let used = 0;
  for (const line of picked) {
    if (used + line.length + 1 > budget) continue;
    shown.push(line);
    used += line.length + 1;
  }
  const header = `Error-like lines in the omitted part: ${matching} matching, ${distinct.length} distinct, showing ${shown.length}${shown.length ? ':' : '.'}`;
  return `[${header}${shown.length ? `\n${shown.join('\n')}` : ''}\n]`;
}

/** Never ends on half of a surrogate pair. */
function whole(textValue, side) {
  if (side === 'end' && /[\uD800-\uDBFF]$/.test(textValue)) return textValue.slice(0, -1);
  if (side === 'start' && /^[\uDC00-\uDFFF]/.test(textValue)) return textValue.slice(1);
  return textValue;
}

function snapHead(text, size) {
  const cut = text.lastIndexOf('\n', size);
  return whole(cut > size * 0.75 ? text.slice(0, cut) : text.slice(0, size), 'end');
}

function snapTail(text, size) {
  const tail = text.slice(text.length - size);
  const cut = tail.indexOf('\n');
  return whole(cut >= 0 && cut < size * 0.25 ? tail.slice(cut + 1) : tail, 'start');
}

function openPrivateDir(dir, ownerUid) {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  // O_NOFOLLOW: a symlink in place of the directory is refused, not followed.
  const fd = fs.openSync(dir, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW);
  try {
    const st = fs.fstatSync(fd);
    if (!st.isDirectory() || st.uid !== ownerUid) throw Object.assign(new Error('unsafe directory'), { code: 'EUNSAFE' });
    if (st.mode & 0o077) fs.fchmodSync(fd, 0o700);
  } catch (err) {
    fs.closeSync(fd);
    throw err;
  }
  return fd;
}

/** Removes result files this module wrote that are older than the retention. Symlinks and strangers stay. */
function sweep(dir, nowMs) {
  for (const name of fs.readdirSync(dir)) {
    if (!RESULT_FILE.test(name)) continue;
    try {
      const st = fs.lstatSync(path.join(dir, name));
      if (st.isFile() && nowMs - st.mtimeMs > RETENTION_MS) fs.unlinkSync(path.join(dir, name));
    } catch (_) {}
  }
}

function writeAll(fd, text) {
  const buf = Buffer.from(text, 'utf8');
  let off = 0;
  while (off < buf.length) off += fs.writeSync(fd, buf, off, buf.length - off);
}

function createExecResultCap(options = {}) {
  const dir = options.dir !== undefined ? options.dir : DEFAULT_DIR;
  const ownerUid = options.ownerUid !== undefined ? options.ownerUid : DEFAULT_OWNER_UID;
  const now = options.now || Date.now;
  const randomHex = options.randomHex || (() => crypto.randomBytes(8).toString('hex'));
  // A log that fails must not fail the result; and it only ever receives error codes, never output.
  const log = (message) => { try { (options.log || (() => {}))(message); } catch (_) {} };
  let lastSweep = 0;

  /** Saves the text RMP saw; returns { file } or { error } and never throws. */
  function save(text, partial) {
    let dirFd = null;
    try {
      // The tree is created only by the user that is required to own it, so it can never be user-owned while called root-only.
      if (process.geteuid() !== ownerUid) throw Object.assign(new Error('not the required owner'), { code: 'EOWNER' });
      dirFd = openPrivateDir(dir, ownerUid);
      const stamp = now();
      if (stamp - lastSweep >= SWEEP_INTERVAL_MS) {
        lastSweep = stamp;
        try { sweep(dir, stamp); } catch (_) {}
      }
      const file = path.join(dir, `exec-${stamp}-${randomHex()}${partial ? '.partial' : ''}.txt`);
      const fd = fs.openSync(file, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
      try {
        const st = fs.fstatSync(fd);
        if (!st.isFile() || st.uid !== ownerUid) throw Object.assign(new Error('unsafe file'), { code: 'EUNSAFE' });
        fs.fchmodSync(fd, 0o600);
        writeAll(fd, text);
      } finally {
        fs.closeSync(fd);
      }
      return { file };
    } catch (err) {
      const code = err && /^[A-Z0-9_]{3,20}$/.test(String(err.code)) ? err.code : 'ERROR';
      log(`exec result cap: not saved (${code})`);
      return { error: code };
    } finally {
      if (dirFd !== null) { try { fs.closeSync(dirFd); } catch (_) {} }
    }
  }

  function build(result, text) {
    const details = result.details;
    const total = text.length;
    const partial = (details && details.truncated === true) || total >= UPSTREAM_LIMIT_CHARS;

    const marker = (omitted) => `[... ${omitted} chars omitted ...]`;
    const budget = MAX_RESULT_CHARS - NOTICE_RESERVE - marker(total).length - 2;
    const head = snapHead(text, Math.floor(budget * 0.6));
    const tail = snapTail(text, budget - Math.floor(budget * 0.6));
    const omitted = total - head.length - tail.length;

    const saved = save(text, partial);
    const status = exitStatus(details, text);
    const lines = [
      `[RMP output cap: this command printed ${total} chars; the first ${head.length} and last ${tail.length} are shown, ${omitted} in between are omitted.]`,
    ];
    if (partial) {
      lines.push(
        `[PARTIAL: OpenClaw truncates exec output to about ${UPSTREAM_LIMIT_CHARS} chars before RMP sees it, and this result is at that limit or flagged truncated by OpenClaw, so the end of the output, errors and the exit status after that point may be missing, and RMP cannot recover them.]`,
      );
    }
    lines.push(`[Exit status: ${status || `unavailable${partial ? ' (not in the output RMP could see)' : ''}`}.]`);
    const savedNote = saved.file
      ? partial
        ? `[Saved the text RMP saw (${total} chars, partial, NOT the complete raw output) to ${saved.file} (mode 600, deleted after 3 days). Search it with head, tail, grep -n or wc -l; do not print it whole.]`
        : `[Saved the complete output (${total} chars) to ${saved.file} (mode 600, deleted after 3 days). Search it with head, tail, grep -n or wc -l; do not print it whole.]`
      : `[Output NOT saved (${saved.error}); only the shown parts exist. Run the command again with its output bounded (head, tail, grep, wc) if you need more.]`;
    const room = NOTICE_RESERVE - lines.join('\n').length - savedNote.length - 160;
    const errors = errorSummary(text.slice(head.length, total - tail.length), Math.max(0, room));
    if (errors) lines.push(errors);
    lines.push(savedNote);
    return `${lines.join('\n')}\n${head}\n${marker(omitted)}\n${tail}`;
  }

  /** Head and tail only, used when building the full notice failed. Touches nothing but the text. */
  function fallback(text) {
    const note = `[RMP output cap (fallback): this command printed ${text.length} chars; the middle is omitted. Exit status: unavailable. Output was not saved.]`;
    const each = Math.floor((MAX_RESULT_CHARS - note.length - 2) / 2);
    return `${note}\n${whole(text.slice(0, each), 'end')}\n${whole(text.slice(text.length - each), 'start')}`;
  }

  /** The result with its text blocks replaced by one capped block; everything else is carried over. */
  function rebuild(result, shown) {
    const out = {};
    for (const key of Object.keys(result)) {
      try { out[key] = result[key]; } catch (_) {}
    }
    const others = Array.isArray(result.content) ? result.content.filter((b) => !(b && b.type === 'text' && typeof b.text === 'string')) : [];
    out.content = [{ type: 'text', text: shown }, ...others];
    if (out.details && typeof out.details === 'object' && typeof out.details.aggregated === 'string') {
      out.details = { ...out.details, aggregated: shown };
    }
    return out;
  }

  /** What replaces a result that cannot be read: nothing of it is passed on. */
  function withheld() {
    return { content: [{ type: 'text', text: '[RMP output cap: this exec result could not be read safely, so it was withheld. Exit status: unavailable. Run the command again with its output bounded (head, tail, grep, wc).]' }] };
  }

  // Every path below ends in a result of at most MAX_RESULT_CHARS, or in the original only when it is already that short.
  return async function execResultCap(event, ctx) {
    let tool;
    let key;
    try {
      tool = event && event.toolName;
      key = ctx && ctx.sessionKey;
    } catch (_) {
      return undefined; // the host's own event cannot even be read: it is not known to be a task exec
    }
    if (tool !== 'exec' || !isTaskSession(key)) return undefined;

    let result;
    let text;
    try {
      result = event.result;
      text = visibleText(result);
    } catch (err) {
      log(`exec result cap: result unreadable, withheld (${err && err.name})`);
      return { result: withheld() };
    }
    if (text === null || text.length <= MAX_RESULT_CHARS) return undefined;

    let shown;
    try {
      shown = build(result, text);
      if (shown.length > MAX_RESULT_CHARS) throw new RangeError('notice too long');
    } catch (err) {
      log(`exec result cap: fell back to head and tail (${err && err.name})`);
      shown = fallback(text);
    }
    try {
      return { result: rebuild(result, shown) };
    } catch (err) {
      log(`exec result cap: rebuilt without the other fields (${err && err.name})`);
      return { result: { content: [{ type: 'text', text: shown }] } };
    }
  };
}

function register(api, options = {}) {
  if (!api || typeof api.registerAgentToolResultMiddleware !== 'function') return false;
  api.registerAgentToolResultMiddleware(createExecResultCap(options), { runtimes: ['openclaw'], matcher: ['exec'] });
  return true;
}

module.exports = {
  register,
  createExecResultCap,
  isTaskSession,
  MAX_RESULT_CHARS,
  UPSTREAM_LIMIT_CHARS,
  RETENTION_MS,
  DEFAULT_DIR,
  DEFAULT_OWNER_UID,
};
