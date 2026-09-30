				if (currentPending && isCurrentWorkerWorkspacePendingResultOwner(current, currentPending)) await deps.reportWorkspaceResultRecoveryFailure?.({
					sessionId: current.sessionId,
					sessionKey: current.sessionKey,
					agentId: current.agentId,
					error: boundedWorkerError(error)
				});
			} catch {}
		}
	}
	if (cleanupOrphans) {
		const retainedRefs = () => new Set(placements.listPendingWorkspaceResults().flatMap((pending) => pending.stagedResultRef ? [cleanupWorkerWorkspaceResultRef(pending.stagedResultRef)] : []));
		const cleanedWorkspaceRoots = /* @__PURE__ */ new Set();
		for (const placement of placements.list()) try {
			const root = await deps.resolveWorkspacePath(placement);
			if (!cleanedWorkspaceRoots.has(root)) {
				cleanedWorkspaceRoots.add(root);
				await deleteWorkerWorkspaceResultCleanupRefs({
					root,
					retainedRefs
				});
			}
		} catch {}
	}
	return /* @__PURE__ */ new Set([...stagedResultOwners, ...placements.listPendingWorkspaceResults().map((pending) => pending.sessionId)]);
}
//#endregion
