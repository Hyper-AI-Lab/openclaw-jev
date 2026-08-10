const fs = require('fs');
const content = fs.readFileSync('/usr/lib/node_modules/openclaw/dist/plugin-sdk/plugins/types.d.ts', 'utf8');
const lines = content.split('\n');

for (let i = 0; i < lines.length; i++) {
    if (lines[i].includes('message_received')) {
        console.log(lines.slice(Math.max(0, i - 15), Math.min(lines.length, i + 15)).join('\n'));
    }
}
