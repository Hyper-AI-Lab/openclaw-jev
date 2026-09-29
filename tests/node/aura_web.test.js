'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const PLUGIN_DIR = path.resolve(__dirname, '../../plugins/aura_web');
const LIVE_DIR = '/root/.openclaw/plugins/aura_web';
const CLIENT = path.join(PLUGIN_DIR, 'lib/client.js');

const calls = [];
require.cache[CLIENT] = {
  id: CLIENT,
  filename: CLIENT,
  loaded: true,
  exports: {
    langsearchSearch: async (...args) => (calls.push(['langsearch', ...args]), { ok: true }),
    jinaReader: async (...args) => (calls.push(['jina', ...args]), { ok: true }),
    backendsPost: async (route, body) => (calls.push(['post', route, body]), { ok: true }),
    backendsGet: async (route) => (calls.push(['get', route]), { ok: true }),
    langsearchKey: () => 'key',
    loadConfig: () => ({}),
    toolText: (payload) => JSON.stringify(payload),
  },
};

function registeredTools() {
  const tools = {};
  require(path.join(PLUGIN_DIR, 'index.js')).register({
    registerTool: (tool) => { tools[tool.name] = tool; },
    registerWebFetchProvider: () => {},
  });
  return tools;
}

test('every tool reads its arguments the way OpenClaw passes them: execute(toolCallId, params)', async () => {
  const tools = registeredTools();
  const url = 'https://example.org/page';
  await tools.langsearch_search.execute('call-1', { query: 'bitcoin price', count: 3 });
  await tools.jina_reader.execute('call-2', { url });
  await tools.crawl4ai.execute('call-3', { url, depth: 1 });
  await tools.scrapling.execute('call-4', { url, css: 'main' });
  await tools.crawlee_crawl.execute('call-5', { url });
  await tools.crawlee_crawl.execute('call-6', { job_id: 'job 7' });
  await tools.scrapegraph_extract.execute('call-7', { url, prompt: 'price', schema_json: '{"type":"object"}' });
  await tools.browser_use.execute('call-8', { task: 'find the price', start_url: url });
  await tools.obscura_browse.execute('call-9', { url });
  assert.deepEqual(calls, [
    ['langsearch', 'bitcoin price', 3],
    ['jina', url],
    ['post', '/v1/crawl4ai', { url, depth: 1, max_pages: 1 }],
    ['post', '/v1/scrapling', { url, css: 'main' }],
    ['post', '/v1/crawlee', { url, max_pages: 10, max_depth: 2 }],
    ['get', '/v1/crawlee/job%207'],
    ['post', '/v1/scrapegraph', { url, prompt: 'price', schema: { type: 'object' } }],
    ['post', '/v1/browser-use', { task: 'find the price', start_url: url }],
    ['post', '/v1/obscura', { url, action: 'fetch', selector: '', text: '', key: '' }],
  ]);
});

test('the live plugin copy matches the repo', () => {
  for (const file of ['index.js', 'lib/client.js']) {
    const live = path.join(LIVE_DIR, file);
    if (fs.existsSync(live)) {
      assert.equal(fs.readFileSync(live, 'utf8'), fs.readFileSync(path.join(PLUGIN_DIR, file), 'utf8'), file);
    }
  }
});
