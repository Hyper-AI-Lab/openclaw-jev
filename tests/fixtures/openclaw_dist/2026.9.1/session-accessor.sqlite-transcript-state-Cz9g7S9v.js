		"session_nodes.fork_source_session_key",
		"session_nodes.parent_session_key",
		"session_nodes.spawned_by",
		"retained_window.session_id as retained_window_id"
	]).orderBy("session_nodes.session_key"))) {
		if (row.entry_json === "{}" && row.entry_valid === -1 && row.retained_window_id === row.current_session_id) continue;
		if (row.entry_valid !== 1) throw canonicalSessionKeyMigrationRequiredError(`invalid persisted session row requires repair for ${row.session_key}`);
		const record = parseSqliteSessionEntryRecord(row);
		if (!record) throw canonicalSessionKeyMigrationRequiredError(`invalid persisted session row requires repair for ${row.session_key}`);
		const entry = projectCanonicalSessionEntryShape(record);
		if ((row.parent_session_key ?? void 0) !== (entry.parentSessionKey ?? entry.spawnedBy ?? void 0) || (row.spawned_by ?? void 0) !== (entry.spawnedBy ?? void 0) || (row.fork_source_session_key ?? void 0) !== (entry.forkSource?.sessionKey ?? void 0)) throw canonicalSessionKeyMigrationRequiredError(`invalid persisted session row requires repair for ${row.session_key}`);
		const deliveryCanonicalKey = resolveDeliveryProvenCanonicalSessionKey(row.session_key, entry);
		if (deliveryCanonicalKey !== row.session_key) throw canonicalSessionKeyMigrationRequiredError(`non-canonical persisted row resolves to session key ${deliveryCanonicalKey}`);

// ---- fixture cut ----

				if (!(candidate.entry_json === "{}" ? executeSqliteQueryTakeFirstSync(database.db, db.selectFrom("session_windows").select("session_id").where("session_id", "=", candidate.current_session_id).where("session_key", "=", candidate.session_key)) : void 0)) throw canonicalSessionKeyMigrationRequiredError(`invalid persisted session row requires repair for ${candidate.session_key}`);
				continue;
			}
			if (resolveDeliveryProvenCanonicalSessionKey(candidate.session_key, entry) !== candidate.session_key) throw canonicalSessionKeyMigrationRequiredError(`non-canonical persisted row resolves to session key ${candidate.session_key}`);
		}
		const existing = candidates.find((candidate) => candidate.session_key === scope.sessionKey);
		if (existing && existing.entry_valid !== 1) {
			if (!(existing.entry_json === "{}" ? executeSqliteQueryTakeFirstSync(database.db, db.selectFrom("session_windows").select("session_id").where("session_id", "=", existing.current_session_id).where("session_key", "=", scope.sessionKey)) : void 0)) throw canonicalSessionKeyMigrationRequiredError(`invalid persisted session row requires repair for ${scope.sessionKey}`);
		}
	}
	if ((executeSqliteQuerySync(database.db, db.insertInto("session_nodes").values({
		session_key: scope.sessionKey,
		current_session_id: scope.sessionId,
