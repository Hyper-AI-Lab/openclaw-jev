const fs = require('fs');

const adapterPath = '/root/.openclaw/plugins/rmp_adapter/index.js';
const newContent = `const child_process = require('child_process');
const fs = require('fs');

module.exports = {
  name: "rmp_adapter",
  register: (api) => {
    // ... Tools omitted ...
    
    api.on("before_message_write", (event, ctx) => {
       fs.appendFileSync('/tmp/hook_debug.log', "CTX: " + JSON.stringify(ctx) + "\\n");
    });
  }
};
`;
fs.writeFileSync(adapterPath, newContent);
