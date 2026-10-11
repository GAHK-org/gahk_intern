import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from django.conf import settings
from django.core.files.storage import FileSystemStorage, storages

from core.storage import MediaS3Storage


class DocumentStore:
    """Private objects outside the media prefix, with no browser-facing URLs."""

    def __init__(self) -> None:
        self._storage = storages["default"]

    def save(self, key: str, source: BinaryIO) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        if isinstance(self._storage, MediaS3Storage):

            class HashingReader:
                def read(inner_self, length: int = -1) -> bytes:
                    nonlocal size
                    chunk = source.read(length)
                    if chunk:
                        digest.update(chunk)
                        size += len(chunk)
                    return chunk

            self._storage.bucket.upload_fileobj(HashingReader(), key)
            return size, digest.hexdigest()

        local = self._local_path(key)
        local.parent.mkdir(parents=True, exist_ok=True)
        with local.open("wb") as destination:
            while chunk := source.read(512 * 1024):
                digest.update(chunk)
                size += len(chunk)
                destination.write(chunk)
        return size, digest.hexdigest()

    def copy(self, source_key: str, target_key: str) -> tuple[int, str]:
        if isinstance(self._storage, MediaS3Storage):
            self._storage.bucket.copy({"Bucket": self._storage.bucket_name, "Key": source_key}, target_key)
            head = self._storage.connection.meta.client.head_object(
                Bucket=self._storage.bucket_name, Key=target_key
            )
            return int(head["ContentLength"]), str(head.get("ETag", "")).strip('"')
        with self._local_path(source_key).open("rb") as source:
            return self.save(target_key, source)

    def delete(self, key: str) -> None:
        if isinstance(self._storage, MediaS3Storage):
            self._storage.connection.meta.client.delete_object(Bucket=self._storage.bucket_name, Key=key)
        else:
            self._local_path(key).unlink(missing_ok=True)

    def signed_download_url(self, key: str, expires_in: int) -> str:
        if not isinstance(self._storage, MediaS3Storage):
            raise RuntimeError("A document server needs S3-compatible object storage.")
        endpoint = settings.DOCUMENT_S3_ENDPOINT_URL
        client = self._storage.connection.meta.client
        if endpoint != self._storage.endpoint_url:
            client = self._storage._create_session().client(
                "s3",
                region_name=self._storage.region_name,
                endpoint_url=endpoint,
                config=self._storage.client_config,
            )
        return client.generate_presigned_url(
            "get_object", Params={"Bucket": self._storage.bucket_name, "Key": key}, ExpiresIn=expires_in
        )

    def _local_path(self, key: str) -> Path:
        if not isinstance(self._storage, FileSystemStorage):
            raise RuntimeError("No supported document storage is configured.")
        return Path(self._storage.path(key))

    def chunks(self, key: str, size: int = 512 * 1024) -> Iterator[bytes]:
        if isinstance(self._storage, MediaS3Storage):
            body = self._storage.connection.meta.client.get_object(Bucket=self._storage.bucket_name, Key=key)[
                "Body"
            ]
            try:
                while chunk := body.read(size):
                    yield chunk
            finally:
                body.close()
            return
        with self._local_path(key).open("rb") as source:
            yield from iter(lambda: source.read(size), b"")


def get_store() -> DocumentStore:
    return DocumentStore()
