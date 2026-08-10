const fs = require('fs');

const adapterPath = '/root/.openclaw/plugins/rmp_adapter/index.js';
let content = fs.readFileSync(adapterPath, 'utf8');

const filterLogic = `
       if (text.includes("[SYSTEM ENFORCEMENT]") || text.includes("[INTERNAL_RMP]")) return;
       if (text.startsWith("System:") || text.startsWith("[cron:") || text.includes("Hook Hook (error)")) return;
       // Also ignore some other system-like things if any
`;

content = content.replace(/if \(text\.includes\("\[SYSTEM ENFORCEMENT\]"\) \|\| text\.includes\("\[INTERNAL_RMP\]"\)\) return;/, filterLogic);

fs.writeFileSync(adapterPath, content);
