const fs = require('fs');

const adapterPath = '/root/.openclaw/plugins/rmp_adapter/index.js';
const newContent = `const child_process = require('child_process');
const fs = require('fs');

module.exports = {
  name: "rmp_adapter",
  register: (api) => {
    api.registerTool({
      name: "rmp_task_create",
      description: "Create a durable task in the Reliability and Memory Plane (RMP).",
      parameters: {
        type: "object",
        properties: {
          intent: { type: "string" },
          task_type: { type: "string" },
          tags: { type: "array", items: { type: "string" } }
        },
        required: ["intent", "task_type"]
      },
      execute: async (params, context) => {
        try {
          const response = await fetch("http://127.0.0.1:8000/tasks", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              intent: params.intent,
              tags: params.tags || [],
              user_id: context?.session?.origin?.from || "unknown_user",
              session_key: context?.sessionKey || "agent:main:main",
              raw_text: params.intent
            })
          });
          if (!response.ok) throw new Error(\`API error: \${response.status}\`);
          const result = await response.json();
          return \`Task successfully created in RMP. Task ID: \${result.task_id}.\`;
        } catch (err) {
          return \`Failed to create task: \${err.message}\`;
        }
      }
    });

    api.registerTool({
      name: "rmp_task_status",
      description: "Check the status of a durable task running in the RMP.",
      parameters: {
        type: "object",
        properties: {
          task_id: { type: "string" }
        },
        required: ["task_id"]
      },
      execute: async (params) => {
        try {
          const response = await fetch(\`http://127.0.0.1:8000/tasks/\${params.task_id}\`);
          if (!response.ok) throw new Error(\`API error: \${response.status}\`);
          const result = await response.json();
          return \`Task \${result.task_id} status: \${result.status}\`;
        } catch (err) {
          return \`Failed to fetch task status: \${err.message}\`;
        }
      }
    });

    api.on("before_message_write", (event) => {
       fs.appendFileSync('/tmp/hook_debug.log', JSON.stringify(event) + "\\n");
       const msg = event.message;
       if (!msg || msg.role !== "user") return;
       
       const text = typeof msg.content === 'string' ? msg.content : (Array.isArray(msg.content) ? msg.content.map(c => c.text).join(' ') : "");
       if (!text) return;
       
       const sessionKey = event.sessionKey || "agent:main:main";

       if (text.includes("[SYSTEM ENFORCEMENT]") || text.includes("[INTERNAL_RMP]")) return;

       try {
          const payload = JSON.stringify({
              intent: \`[AUTO-ROUTED]: \${text.substring(0, 100).replace(/'/g, "")}...\`,
              tags: ["auto-routed", "audit"],
              user_id: "slack-user",
              session_key: sessionKey,
              raw_text: text.replace(/'/g, "")
          });
          
          fs.appendFileSync('/tmp/hook_debug.log', "Triggering curl for RMP\\n");
          const out = child_process.execSync("curl -s -X POST http://127.0.0.1:8000/tasks -H \\"Content-Type: application/json\\" -d @-", { input: payload });
          const data = JSON.parse(out.toString());
          fs.appendFileSync('/tmp/hook_debug.log', "Curl success: " + JSON.stringify(data) + "\\n");
          
          if (data.task_id) {
            return {
              message: {
                 ...msg,
                 content: \`[SYSTEM ENFORCEMENT]: A backend orchestrator task has been automatically created for this request (Task ID: \${data.task_id}). You MUST use the rmp_task_status tool to monitor it or the rmp_task_create tool if you need to spawn further steps. DO NOT answer directly from your knowledge base. Just acknowledge that the task has been dispatched to the Reliability and Memory Plane.\\n\\nUser Request: \${text}\`
              }
            };
          }
       } catch (err) {
          fs.appendFileSync('/tmp/hook_debug.log', "Error: " + err.message + "\\n");
          if (api.logger) api.logger.error("Failed to auto-route message: " + err.message);
       }
    });

  }
};
`;

fs.writeFileSync(adapterPath, newContent);
