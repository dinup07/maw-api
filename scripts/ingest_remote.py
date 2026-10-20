#!/usr/bin/env python3
"""Ingest text/Markdown files into a MAW vector store over the HTTP API (stdlib only).

    python3 scripts/ingest_remote.py docs_gemini examples/docs/*.md \
        --base https://203-0-113-7.sslip.io --api-key "$MAW_KEY"

Document ids are the file paths, so re-running replaces documents instead of duplicating them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BATCH = 50


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("store")
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--base", default=os.environ.get("MAW_BASE", "http://localhost:8000"))
    parser.add_argument("--api-key", default=os.environ.get("MAW_API_KEY"))
    parser.add_argument("--chunk-size", type=int, default=1000)
    parser.add_argument("--chunk-overlap", type=int, default=150)
    args = parser.parse_args()

    docs = [
        {"id": path.as_posix(), "text": path.read_text(encoding="utf-8"), "metadata": {"source": path.name}}
        for path in args.files
        if path.is_file()
    ]
    if not docs:
        print("no readable files", file=sys.stderr)
        return 1
    headers = {"content-type": "application/json"}
    if args.api_key:
        headers["x-api-key"] = args.api_key
    url = f"{args.base.rstrip('/')}/v1/vector-stores/{args.store}/documents"
    total = 0
    for start in range(0, len(docs), BATCH):
        body = {
            "documents": docs[start : start + BATCH],
            "chunk_size": args.chunk_size,
            "chunk_overlap": args.chunk_overlap,
        }
        request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=300) as resp:
                result = json.load(resp)
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code}: {e.read().decode()[:500]}", file=sys.stderr)
            return 1
        total += result["chunks"]
        print(f"ingested {result['documents']} documents ({result['chunks']} chunks)")
    print(f"done: {len(docs)} documents, {total} chunks into '{args.store}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
