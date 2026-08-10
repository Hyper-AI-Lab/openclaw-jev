#!/usr/bin/env python3
"""Copy vectors from embedded Qdrant storage to a running Qdrant server."""
from __future__ import annotations

import argparse
import sys

from qdrant_client import QdrantClient
from qdrant_client.http import models as rest


def _to_point(record: rest.Record) -> rest.PointStruct:
    return rest.PointStruct(
        id=record.id,
        vector=record.vector,
        payload=record.payload or {},
    )


def scroll_all(client: QdrantClient, collection: str, batch: int = 100):
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=batch,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        if not points:
            break
        yield points
        if offset is None:
            break


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--embedded-path", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6333)
    parser.add_argument("--collection", default="rmp_memories")
    args = parser.parse_args()

    local = QdrantClient(path=args.embedded_path)
    remote = QdrantClient(host=args.host, port=args.port, check_compatibility=False)

    if not local.collection_exists(args.collection):
        print(f"No embedded collection {args.collection!r}; nothing to migrate.")
        return 0

    info = local.get_collection(args.collection)
    vector_size = info.config.params.vectors.size
    print(f"Embedded collection {args.collection}: dim={vector_size}")

    if remote.collection_exists(args.collection):
        remote.delete_collection(args.collection)
    remote.create_collection(
        collection_name=args.collection,
        vectors_config=rest.VectorParams(size=vector_size, distance=rest.Distance.COSINE),
    )

    total = 0
    for batch in scroll_all(local, args.collection):
        points = [_to_point(p) for p in batch]
        remote.upsert(collection_name=args.collection, points=points, wait=True)
        total += len(batch)
        print(f"  migrated {total} points...")

    print(f"Done: {total} points → {args.host}:{args.port}/{args.collection}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
