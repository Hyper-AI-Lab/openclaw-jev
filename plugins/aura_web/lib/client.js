const { execFileSync } = require('child_process');
const fs = require('fs');

const OPENCLAW_JSON = '/root/.openclaw/openclaw.json';
const DEFAULT_BACKENDS = 'http://127.0.0.1:8791';
const LANGSEARCH_URL = 'https://api.langsearch.com/v1/web-search';
const JINA_BASE = 'https://r.jina.ai';

function loadConfig() {
  try {
    return JSON.parse(fs.readFileSync(OPENCLAW_JSON, 'utf8'));
  } catch (_) {
    return {};
  }
}

function pluginConfig(cfg) {
  const entry = cfg?.plugins?.entries?.aura_web || {};
  return entry.config || {};
}

function backendsUrl(cfg) {
  const fromPlugin = pluginConfig(cfg).backendsUrl;
  return (process.env.AURA_WEB_BACKENDS_URL || fromPlugin || DEFAULT_BACKENDS).replace(/\/$/, '');
}

function langsearchKey(cfg) {
  const env = (process.env.LANGSEARCH_API_KEY || '').trim();
  if (env) return env;
  const key = cfg?.plugins?.entries?.langsearch?.config?.webSearch?.apiKey;
  if (!key || key === '<>') return '';
  return String(key).trim();
}

function jinaKey(cfg) {
  const env = (process.env.JINA_API_KEY || '').trim();
  if (env) return env;
  const key = pluginConfig(cfg).jina?.apiKey;
  if (!key || key === '<>') return '';
  return String(key).trim();
}

function curlJson(method, url, body, headers, maxTimeSec) {
  const args = [
    '-sS',
    '-X', method,
    '-H', 'Content-Type: application/json',
    '--max-time', String(maxTimeSec || 60),
    '-w', '\n%{http_code}',
  ];
  if (headers) {
    for (const [k, v] of Object.entries(headers)) {
      args.push('-H', `${k}: ${v}`);
    }
  }
  if (body !== undefined) {
    args.push('-d', JSON.stringify(body));
  }
  args.push(url);
  const raw = execFileSync('/usr/bin/curl', args, { encoding: 'utf8', maxBuffer: 8 * 1024 * 1024 });
  const nl = raw.lastIndexOf('\n');
  const httpCode = raw.slice(nl + 1).trim();
  const text = raw.slice(0, nl);
  let data = null;
  try {
    data = text ? JSON.parse(text) : {};
  } catch (_) {
    data = { raw: text };
  }
  return { httpCode, data, text };
}

function toolText(payload) {
  if (typeof payload === 'string') return payload;
  return JSON.stringify(payload, null, 2);
}

async function langsearchSearch(query, count) {
  const cfg = loadConfig();
  const apiKey = langsearchKey(cfg);
  if (!apiKey) {
    return {
      ok: false,
      error: 'LangSearch API key missing. Set plugins.entries.langsearch.config.webSearch.apiKey in openclaw.json (replace <>).',
    };
  }
  const { httpCode, data, text } = curlJson(
    'POST',
    LANGSEARCH_URL,
    {
      query,
      freshness: 'noLimit',
      summary: true,
      count: Math.min(Math.max(Number(count) || 8, 1), 10),
    },
    { Authorization: `Bearer ${apiKey}` },
    45
  );
  if (!httpCode.startsWith('2')) {
    return { ok: false, error: `LangSearch HTTP ${httpCode}`, detail: data || text };
  }
  const pages = data?.data?.webPages?.value || data?.webPages?.value || [];
  const results = pages.map((p, i) => ({
    rank: i + 1,
    title: p.name || p.title || '',
    url: p.url || '',
    snippet: p.snippet || p.summary || p.description || '',
  }));
  return { ok: true, provider: 'langsearch', query, results, raw_code: data?.code };
}

async function jinaReader(url) {
  const cfg = loadConfig();
  const apiKey = jinaKey(cfg);
  const base = (pluginConfig(cfg).jina?.baseUrl || JINA_BASE).replace(/\/$/, '');
  // Jina Reader: https://r.jina.ai/https://example.com
  const fetchUrl = `${base}/${url}`;
  const args = [
    '-sS',
    '-L',
    '--max-time', '60',
    '-H', 'Accept: text/plain',
    '-w', '\n%{http_code}',
  ];
  if (apiKey) {
    args.push('-H', `Authorization: Bearer ${apiKey}`);
  }
  args.push(fetchUrl);
  const raw = execFileSync('/usr/bin/curl', args, { encoding: 'utf8', maxBuffer: 8 * 1024 * 1024 });
  const nl = raw.lastIndexOf('\n');
  const httpCode = raw.slice(nl + 1).trim();
  const text = raw.slice(0, nl);
  if (!httpCode.startsWith('2')) {
    return { ok: false, error: `Jina HTTP ${httpCode}`, detail: text.slice(0, 500) };
  }
  return { ok: true, provider: 'jina', url, markdown: text.slice(0, 120000) };
}

async function backendsPost(path, body, maxTimeSec) {
  const cfg = loadConfig();
  const base = backendsUrl(cfg);
  try {
    const { httpCode, data, text } = curlJson('POST', `${base}${path}`, body, null, maxTimeSec || 90);
    if (!httpCode.startsWith('2')) {
      return { ok: false, error: `backends ${path} HTTP ${httpCode}`, detail: data || text };
    }
    return data;
  } catch (err) {
    return {
      ok: false,
      error: `web-stack backends unreachable at ${base}: ${err.message}`,
      hint: 'Start: systemctl start aura-web-stack  OR  /root/.openclaw/web-stack/run-local.sh',
    };
  }
}

async function backendsGet(path) {
  const cfg = loadConfig();
  const base = backendsUrl(cfg);
  try {
    const raw = execFileSync(
      '/usr/bin/curl',
      ['-sS', '--max-time', '15', '-w', '\n%{http_code}', `${base}${path}`],
      { encoding: 'utf8', maxBuffer: 2 * 1024 * 1024 }
    );
    const nl = raw.lastIndexOf('\n');
    const httpCode = raw.slice(nl + 1).trim();
    const text = raw.slice(0, nl);
    let data = null;
    try {
      data = text ? JSON.parse(text) : {};
    } catch (_) {
      data = { raw: text };
    }
    if (!httpCode.startsWith('2')) {
      return { ok: false, error: `backends GET ${path} HTTP ${httpCode}`, detail: data };
    }
    return data;
  } catch (err) {
    return { ok: false, error: `web-stack backends unreachable: ${err.message}` };
  }
}

module.exports = {
  langsearchSearch,
  jinaReader,
  backendsPost,
  backendsGet,
  langsearchKey,
  loadConfig,
  toolText,
};
