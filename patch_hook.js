const fs = require('fs');

const adapterPath = '/root/.openclaw/plugins/rmp_adapter/index.js';
let content = fs.readFileSync(adapterPath, 'utf8');

// Replace the overly broad System filter
content = content.replace(
    /if \(text\.startsWith\("System:"\) \|\| text\.startsWith\("\[cron:"\) \|\| text\.includes\("Hook Hook \(error\)"\)\) return;/g,
    'if (text.startsWith("System:") && !text.includes("Slack DM from")) return;\n       if (text.startsWith("[cron:") || text.includes("Hook Hook (error)")) return;'
);

fs.writeFileSync(adapterPath, content, 'utf8');
console.log("Hook patched.");
