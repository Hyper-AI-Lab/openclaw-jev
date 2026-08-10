const fs = require('fs');

const adapterPath = '/root/.openclaw/plugins/rmp_adapter/index.js';
const newContent = `module.exports = {
  name: "rmp_adapter",
  register: (api) => {
    // 1. Tool to allow the agent to explicitly spawn an RMP sub-process task
    api.registerTool({
      name: "rmp_task_create",
      description: "Create a durable task in the Reliability and Memory Plane (RMP). Use this for any non-trivial user request that requires multiple steps, retries, or long-running execution.",
      parameters: {
        type: "object",
        properties: {
          intent: { type: "string", description: "The overarching goal or request from the user." },
          task_type: { type: "string", description: "Categorized task type (e.g. web_registration, research, coding)." },
          tags: { type: "array", items: { type: "string" }, description: "Tags to attach for memory routing." }
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

          if (!response.ok) {
            throw new Error(\`RMP API returned \${response.status}: \${await response.text()}\`);
          }

          const result = await response.json();
          return \`Task successfully created in RMP. Task ID: \${result.task_id}. The orchestrator will now manage its execution.\`;
        } catch (err) {
          return \`Failed to create task in RMP: \${err.message}\`;
        }
      }
    });

    api.registerTool({
      name: "rmp_task_status",
      description: "Check the status of a durable task running in the RMP.",
      parameters: {
        type: "object",
        properties: {
          task_id: { type: "string", description: "The ID of the task." }
        },
        required: ["task_id"]
      },
      execute: async (params) => {
        try {
          const response = await fetch(\`http://127.0.0.1:8000/tasks/\${params.task_id}\`);
          if (!response.ok) throw new Error(\`RMP API returned \${response.status}\`);
          const result = await response.json();
          return \`Task \${result.task_id} status: \${result.status}\`;
        } catch (err) {
          return \`Failed to fetch task status: \${err.message}\`;
        }
      }
    });

    // 2. Hook to intercept incoming messages and force routing to RMP
    api.registerHook("message:received", async (event) => {
       const text = event.context?.content;
       if (!text) return;
       
       const userId = event.context?.from || "unknown_user";
       const sessionKey = event.sessionKey || "agent:main:main";

       // Avoid endless loops if the LLM is responding to a tool result
       if (text.includes("[SYSTEM ENFORCEMENT]")) return;

       try {
          const response = await fetch("http://127.0.0.1:8000/tasks", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              intent: \`[AUTO-ROUTED]: \${text.substring(0, 100)}...\`,
              tags: ["auto-routed", "audit"],
              user_id: userId,
              session_key: sessionKey,
              raw_text: text
            })
          });
          
          if (response.ok) {
            const data = await response.json();
            // Override the message content before the agent sees it
            event.context.content = \`[SYSTEM ENFORCEMENT]: A backend orchestrator task has been automatically created for this request (Task ID: \${data.task_id}). You MUST use the rmp_task_status tool to monitor it or the rmp_task_create tool if you need to spawn further steps. DO NOT answer directly from your knowledge base. Just acknowledge that the task has been dispatched to the Reliability and Memory Plane.\\n\\nUser Request: \${text}\`;
          }
       } catch (err) {
          if (api.logger) api.logger.error("Failed to auto-route message to RMP: " + err.message);
          else console.error("Failed to auto-route message to RMP: " + err.message);
       }
    }, { name: "rmp-auto-router" });

  }
};
`;

fs.writeFileSync(adapterPath, newContent);
