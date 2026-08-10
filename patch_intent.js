const fs = require('fs');

const adapterPath = '/root/.openclaw/plugins/rmp_adapter/index.js';
let content = fs.readFileSync(adapterPath, 'utf8');

const find = `const payload = JSON.stringify({
              intent: \`[AUTO-ROUTED]: \${text.substring(0, 100).replace(/'/g, "")}...\`,`;

const replace = `let cleanText = text;
          if (text.includes("Conversation info")) {
             const parts = text.split("\\n\\n");
             cleanText = parts[parts.length - 1]; // user query is always at the end
          }
          const payload = JSON.stringify({
              intent: \`[AUTO-ROUTED]: \${cleanText.substring(0, 100).replace(/'/g, "")}...\`,`;

content = content.replace(find, replace);
fs.writeFileSync(adapterPath, content, 'utf8');
console.log("Intent patched.");
