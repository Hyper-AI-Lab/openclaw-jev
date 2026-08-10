#!/bin/bash
# OBSOLETE: Do not rewrite failover decisions to surface_error.
# RMP architecture intentionally uses OpenClaw model fallbacks
# (MiniMax → DeepSeek Flash → GLM-5.2). Kept as a no-op for old docs/scripts.
echo "apply_patch.sh: skipped (model fallbacks enabled by design)"
exit 0
