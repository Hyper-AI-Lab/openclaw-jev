'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const PLUGINS = path.resolve(__dirname, '../../plugins');

function registered(entry, env) {
  const script = `
    const plugin = require(${JSON.stringify(entry)});
    const providers = [];
    plugin.register({ registerWebSearchProvider: (p) => providers.push(p.id), logger: { warn: (m) => providers.push('warn:' + m) } });
    process.stdout.write(JSON.stringify(providers));
  `;
  return JSON.parse(execFileSync(process.execPath, ['-e', script], { env: { ...process.env, ...env } }).toString());
}

test('langsearch registers when loaded in place next to aura_web (2026.9.1)', () => {
  assert.deepEqual(registered(path.join(PLUGINS, 'langsearch', 'index.js'), {}), ['langsearch']);
});

test('langsearch registers from a captured copy without its sibling plugins (2026.9.7)', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'plugin-capture-'));
  const captured = path.join(root, 'captures', 'openclaw-plugin-build-x', 'package-0', 'node_modules', 'langsearch');
  fs.mkdirSync(captured, { recursive: true });
  fs.copyFileSync(path.join(PLUGINS, 'langsearch', 'index.js'), path.join(captured, 'index.js'));
  const stateDir = path.join(root, 'state');
  fs.mkdirSync(path.join(stateDir, 'plugins', 'aura_web', 'lib'), { recursive: true });
  fs.copyFileSync(path.join(PLUGINS, 'aura_web', 'lib', 'client.js'), path.join(stateDir, 'plugins', 'aura_web', 'lib', 'client.js'));
  try {
    assert.deepEqual(registered(path.join(captured, 'index.js'), { OPENCLAW_STATE_DIR: stateDir }), ['langsearch']);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
