const fs = require('fs');

const adapterPath = '/root/.openclaw/plugins/rmp_adapter/index.js';
let content = fs.readFileSync(adapterPath, 'utf8');

// Replace the line that blocks crons
content = content.replace(
    /if \(text\.startsWith\("\[cron:"\) \|\| text\.includes\("Hook Hook \(error\)"\)\) return;/g,
    'if (text.includes("Hook Hook (error)")) return;'
);

fs.writeFileSync(adapterPath, content, 'utf8');
console.log("Hook patched for crons.");
