"""Content-addressed artifact object store with SHA-256 checksums."""
import base64
import hashlib
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import Artifact

logger = logging.getLogger("rmp.artifacts")

DEFAULT_ARTIFACT_ROOT = "/root/.openclaw/rmp/data/artifacts"


def get_artifact_root() -> Path:
    from app.config import get_artifact_store_config

    cfg = get_artifact_store_config()
    root = Path(cfg.get("root_path", DEFAULT_ARTIFACT_ROOT))
    root.mkdir(parents=True, exist_ok=True)
    return root


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-]", "_", (name or "blob").strip())[:120]
    return cleaned or "blob"


class ArtifactStore:
    """Filesystem-backed artifact store with Postgres metadata."""

    @staticmethod
    def _storage_rel_path(checksum: str, filename: str) -> str:
        return f"{checksum[:2]}/{checksum}/{_safe_filename(filename)}"

    @staticmethod
    async def store(
        process_run_id: str,
        kind: str,
        data: bytes,
        filename: str = "blob",
        mime_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not process_run_id:
            raise ValueError("process_run_id is required")
        if not data:
            raise ValueError("artifact data is empty")

        checksum = sha256_bytes(data)
        root = get_artifact_root()
        rel_path = ArtifactStore._storage_rel_path(checksum, filename)
        abs_path = root / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        if not abs_path.exists():
            abs_path.write_bytes(data)
        else:
            existing_hash = sha256_bytes(abs_path.read_bytes())
            if existing_hash != checksum:
                raise RuntimeError(f"Storage collision at {rel_path}")

        artifact_id = str(uuid.uuid4())
        uri = f"rmp-artifact://{artifact_id}"

        async with AsyncSessionLocal() as db:
            existing = await db.execute(
                select(Artifact).where(
                    Artifact.process_run_id == process_run_id,
                    Artifact.checksum == checksum,
                    Artifact.kind == kind,
                )
            )
            prior = existing.scalar_one_or_none()
            if prior:
                return ArtifactStore._serialize(prior)

            row = Artifact(
                id=artifact_id,
                process_run_id=process_run_id,
                kind=kind or "blob",
                uri=uri,
                checksum=checksum,
                mime_type=mime_type or "application/octet-stream",
                filename=_safe_filename(filename),
                size_bytes=len(data),
                storage_key=rel_path,
            )
            db.add(row)
            try:
                await db.commit()
            except Exception as e:
                await db.rollback()
                err = str(e).lower()
                if "foreign key" in err or "foreignkeyviolation" in err:
                    raise ValueError(f"process_run_id not found: {process_run_id}") from e
                raise
            await db.refresh(row)
            return ArtifactStore._serialize(row, metadata=metadata)

    @staticmethod
    async def store_text(
        process_run_id: str,
        kind: str,
        text: str,
        filename: str = "output.txt",
        mime_type: str = "text/plain; charset=utf-8",
    ) -> Dict[str, Any]:
        return await ArtifactStore.store(
            process_run_id,
            kind,
            (text or "").encode("utf-8"),
            filename=filename,
            mime_type=mime_type,
        )

    @staticmethod
    async def get_metadata(artifact_id: str) -> Optional[Dict[str, Any]]:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Artifact).where(Artifact.id == artifact_id))
            row = result.scalar_one_or_none()
            if not row:
                return None
            return ArtifactStore._serialize(row)

    @staticmethod
    async def read_content(artifact_id: str, verify: bool = True) -> bytes:
        meta = await ArtifactStore.get_metadata(artifact_id)
        if not meta:
            raise FileNotFoundError(f"Artifact not found: {artifact_id}")

        root = get_artifact_root()
        abs_path = root / meta["storage_key"]
        if not abs_path.exists():
            raise FileNotFoundError(f"Artifact file missing: {meta['storage_key']}")

        data = abs_path.read_bytes()
        if verify:
            actual = sha256_bytes(data)
            if actual != meta["checksum"]:
                raise RuntimeError(
                    f"Checksum mismatch for artifact {artifact_id}: "
                    f"expected {meta['checksum']}, got {actual}"
                )
        return data

    @staticmethod
    async def list_for_process(process_run_id: str) -> List[Dict[str, Any]]:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Artifact)
                .where(Artifact.process_run_id == process_run_id)
                .order_by(Artifact.created_at.desc())
            )
            return [ArtifactStore._serialize(r) for r in result.scalars().all()]

    @staticmethod
    async def verify_process_artifacts(process_run_id: str) -> Dict[str, Any]:
        """Verify all artifacts for a process run still match stored checksums."""
        items = await ArtifactStore.list_for_process(process_run_id)
        issues: List[str] = []
        for item in items:
            try:
                await ArtifactStore.read_content(item["id"], verify=True)
            except Exception as e:
                issues.append(f"{item['id'][:8]} ({item['kind']}): {e}")
        return {"passed": len(issues) == 0, "issues": issues, "count": len(items)}

    @staticmethod
    def _serialize(row: Artifact, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        out = {
            "id": row.id,
            "process_run_id": row.process_run_id,
            "kind": row.kind,
            "uri": row.uri,
            "checksum": row.checksum,
            "mime_type": row.mime_type,
            "filename": getattr(row, "filename", None) or "blob",
            "size_bytes": getattr(row, "size_bytes", None),
            "storage_key": getattr(row, "storage_key", None),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "download_url": f"/artifacts/{row.id}/download",
        }
        if metadata:
            out["metadata"] = metadata
        return out


def decode_content(content: Union[str, bytes], encoding: str = "utf-8") -> bytes:
    if isinstance(content, bytes):
        return content
    if encoding == "base64":
        return base64.b64decode(content)
    return content.encode("utf-8")
