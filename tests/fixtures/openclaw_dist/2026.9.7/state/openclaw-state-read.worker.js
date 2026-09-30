}
function parseSqliteSessionEntryRecord(row) {
	try {
		const record = JSON.parse(row.entry_json);
		if (!isRecord(record)) return null;
		if (!hasValidSessionEntryIdentity(record)) return null;
		if (row.current_session_id !== void 0 && row.current_session_id !== record.sessionId || row.updated_at !== void 0 && row.updated_at !== record.updatedAt) return null;
		return record;
	} catch {
		return null;
	}
}

// ---- fixture cut ----

/** One validator serves full Doctor scans, pending rows, and final writer certification. */
function validateCanonicalSessionRow(row, mode = "admission") {
	const record = row.entry_valid === 1 || mode === "read" && row.entry_valid === 0 ? parseSqliteSessionEntryRecord({
		entry_json: row.entry_json,
		current_session_id: row.current_session_id
	}) : null;
	return validateCanonicalSessionRowEntry(row, record ? projectCanonicalSessionEntryShape(record) : null, mode);
}
/** Exact readers validate the entry decoded from the same selected row. */
function validateCanonicalSessionRowEntry(row, entry, mode = "admission") {
	if (row.entry_json === "{}" && row.entry_valid === -1 && row.retained_window_id === row.current_session_id) return;
	if (!entry || row.entry_valid !== 1 && (mode !== "read" || row.entry_valid !== 0)) throw canonicalSessionKeyMigrationRequiredError(`invalid persisted session row requires repair for ${row.session_key}`);
	if ((row.parent_session_key ?? void 0) !== (entry.parentSessionKey ?? entry.spawnedBy ?? void 0) || (row.spawned_by ?? void 0) !== (entry.spawnedBy ?? void 0) || (row.fork_source_session_key ?? void 0) !== (entry.forkSource?.sessionKey ?? void 0)) throw canonicalSessionKeyMigrationRequiredError(`invalid persisted session row requires repair for ${row.session_key}`);
	const deliveryCanonicalKey = resolveDeliveryProvenCanonicalSessionKey(row.session_key, entry);
	if (deliveryCanonicalKey !== row.session_key) throw canonicalSessionKeyMigrationRequiredError(`non-canonical persisted row resolves to session key ${deliveryCanonicalKey}`);
	const trimmed = row.session_key.trim();
