"""AWS Lambda entrypoint: S3 -> /tmp/data -> daily_runner -> S3."""

from __future__ import annotations

import os
from pathlib import Path

import boto3

from src.live.daily_runner import run_daily

LOCAL_DATA_DIR = Path("/tmp/data")
SYNC_PREFIXES = ("bronze/", "silver/", "gold/", "live/", "industrial/")


def _download_state(s3, bucket: str, prefix: str) -> None:
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or "raw_archives/" in key:
                continue
            rel = key[len(prefix) :].lstrip("/")
            dest = LOCAL_DATA_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(dest))


def _upload_state(s3, bucket: str, prefix: str) -> None:
    for sub in SYNC_PREFIXES:
        sub_dir = LOCAL_DATA_DIR / sub.rstrip("/")
        if not sub_dir.exists():
            continue
        for fpath in sub_dir.rglob("*"):
            if fpath.is_file():
                rel = fpath.relative_to(LOCAL_DATA_DIR).as_posix()
                key = f"{prefix.rstrip('/')}/{rel}" if prefix else rel
                s3.upload_file(str(fpath), bucket, key)


def handler(event: dict, context) -> dict:
    bucket = os.environ["S3_BUCKET"]
    prefix = os.getenv("S3_PREFIX", "osint-alpha/data/").rstrip("/") + "/"
    equity = float(os.getenv("BASE_EQUITY", "25000"))
    skip_ingest = bool(event.get("skip_ingest", False)) if isinstance(event, dict) else False
    dry_run = bool(event.get("dry_run", False)) if isinstance(event, dict) else False

    LOCAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    s3 = boto3.client("s3")

    print(f"[lambda] Syncing state down from s3://{bucket}/{prefix}")
    _download_state(s3, bucket, prefix)

    run_daily(base_equity=equity, skip_ingest=skip_ingest, dry_run=dry_run)

    print(f"[lambda] Syncing updated state up to s3://{bucket}/{prefix}")
    _upload_state(s3, bucket, prefix)
    return {"status": "ok", "bucket": bucket}
