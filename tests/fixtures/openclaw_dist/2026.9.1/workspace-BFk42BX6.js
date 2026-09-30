		if (typeof file.path !== "string") return true;
		const filePath = file.path.trim();
		if (!filePath) return true;
		return (path.isAbsolute(filePath) ? path.resolve(filePath) : filePath.startsWith("~") ? resolveUserPath(filePath) : path.resolve(resolvedWorkspaceRoot, filePath)) !== rootMemoryPath;
	});
}
function filterBootstrapFilesForSession(files, session) {
	const { sessionKey, chatType, workspaceDir } = resolveBootstrapSessionContext(session);
	const isSubagent = isSubagentSessionKey(sessionKey);
	const isCron = isCronSessionKey(sessionKey);
	const effectiveChatType = chatType ?? deriveSessionChatTypeFromKey(sessionKey);
	const privacyFilteredFiles = isSubagent || isCron || effectiveChatType === "group" || effectiveChatType === "channel" ? filterRootMemoryBootstrapFiles(files, workspaceDir) : files;
	if (isSubagent) return privacyFilteredFiles.filter((file) => SUBAGENT_BOOTSTRAP_ALLOWLIST.has(file.name));
