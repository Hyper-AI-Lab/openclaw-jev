const fs = require('fs');

async function trigger() {
  const payload = {
    type: "message",
    action: "received",
    sessionKey: "agent:main:main",
    context: {
      from: "user_123",
      content: "Hello, this is a test message to trigger RMP."
    }
  };

  // We can't trigger internal hooks directly from outside. We have to test this live.
}

trigger();
