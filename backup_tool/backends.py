from __future__ import annotations

import json
import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path


class BackendError(Exception):
    pass


def safe_key(key: str) -> str:
    parts = key.split("/")
    if key.startswith("/") or ".." in parts or "" in parts:
        raise BackendError(f"Unsafe key '{key}'.")
    return key


class Backend(ABC):
    @abstractmethod
    def put(self, key: str, file: Path, metadata: dict[str, str]) -> None: ...

    @abstractmethod
    def get(self, key: str, dest: Path) -> dict[str, str]: ...

    @abstractmethod
    def keys(self) -> list[str]: ...


class FolderBackend(Backend):
    """Any directory: a mounted drive, a NAS share or a synced cloud folder."""

    def __init__(self, root: Path, prefix: str = ""):
        self.root = root
        self.prefix = prefix.strip("/")

    def _path(self, key: str) -> Path:
        full = f"{self.prefix}/{key}" if self.prefix else key
        return self.root / safe_key(full)

    def put(self, key: str, file: Path, metadata: dict[str, str]) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            shutil.copyfile(file, tmp)
            os.replace(tmp, dest)
            meta = dest.with_name(dest.name + ".meta.json")
            meta.write_text(json.dumps(metadata), encoding="utf-8")
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise BackendError(str(e)) from e

    def get(self, key: str, dest: Path) -> dict[str, str]:
        src = self._path(key)
        try:
            shutil.copyfile(src, dest)
            meta = src.with_name(src.name + ".meta.json")
            return json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
        except OSError as e:
            raise BackendError(str(e)) from e

    def keys(self) -> list[str]:
        base = self.root / self.prefix if self.prefix else self.root
        if not base.is_dir():
            return []
        return sorted(p.relative_to(base).as_posix() for p in base.rglob("*.gz") if p.is_file())


class S3Backend(Backend):
    """Any S3-compatible server, for example a self-hosted MinIO. Credentials come from the usual AWS_* environment variables."""

    def __init__(self, bucket: str, prefix: str = "", endpoint_url: str | None = None, client=None):
        if client is None:
            try:
                import boto3
            except ImportError:
                raise BackendError("The s3 backend needs boto3. Install it with: pip install 'cloud-backup-tool[s3]'") from None
            client = boto3.client("s3", endpoint_url=endpoint_url)
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, key: str) -> str:
        return safe_key(f"{self.prefix}/{key}" if self.prefix else key)

    def put(self, key: str, file: Path, metadata: dict[str, str]) -> None:
        try:
            self.client.upload_file(str(file), self.bucket, self._key(key), ExtraArgs={"Metadata": metadata})
        except Exception as e:
            raise BackendError(str(e)) from e

    def get(self, key: str, dest: Path) -> dict[str, str]:
        try:
            self.client.download_file(self.bucket, self._key(key), str(dest))
            return self.client.head_object(Bucket=self.bucket, Key=self._key(key)).get("Metadata", {})
        except Exception as e:
            raise BackendError(str(e)) from e

    def keys(self) -> list[str]:
        out: list[str] = []
        token = None
        strip = len(self.prefix) + 1 if self.prefix else 0
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": self.prefix + "/" if self.prefix else ""}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            out += [o["Key"][strip:] for o in page.get("Contents", []) if o["Key"].endswith(".gz")]
            if not page.get("IsTruncated"):
                return sorted(out)
            token = page["NextContinuationToken"]
