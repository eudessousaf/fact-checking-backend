"""Envio de arquivos para o DigitalOcean Spaces (armazenamento compatível com S3).

O leitor nunca fala com a máquina do operador: ele lê da CDN do Spaces. Esta
máquina só envia. Os arquivos sobem já comprimidos (gzip) e com cache curto
nos JSON, para a CDN servir milhares de leitores sem repassar cada pedido ao
armazenamento — que, sozinho, tem teto documentado de 800 operações/s.
"""

from __future__ import annotations

import gzip
import logging
import mimetypes
from pathlib import Path

log = logging.getLogger("factcheck.hosting")

JSON_CACHE = "public, max-age=15"
STATIC_CACHE = "public, max-age=300"
COMPRESSIBLE = ("text/", "application/json", "application/javascript", "image/svg+xml")


class SpacesUploader:
    def __init__(self, bucket: str, region: str, key: str, secret: str, prefix: str = "", client=None):
        if not (bucket and region and key and secret):
            raise ValueError("SPACES_BUCKET, SPACES_REGION, SPACES_KEY e SPACES_SECRET precisam estar no .env")
        if client is None:
            import boto3

            client = boto3.session.Session().client(
                "s3", region_name=region, endpoint_url=f"https://{region}.digitaloceanspaces.com",
                aws_access_key_id=key, aws_secret_access_key=secret)
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def key_for(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def put(self, name: str, body: bytes, content_type: str, cache_control: str | None = None) -> None:
        extra = {}
        if content_type.startswith(COMPRESSIBLE):
            body = gzip.compress(body, compresslevel=9)
            extra["ContentEncoding"] = "gzip"
        self.client.put_object(
            Bucket=self.bucket, Key=self.key_for(name), Body=body, ACL="public-read",
            ContentType=content_type,
            CacheControl=cache_control or (JSON_CACHE if name.endswith(".json") else STATIC_CACHE),
            **extra)

    def public_url(self, name: str) -> str:
        return f"https://{self.bucket}.{self.client.meta.region_name}.cdn.digitaloceanspaces.com/{self.key_for(name)}"


def content_type_of(path: Path) -> str:
    if path.suffix == ".js":
        return "application/javascript; charset=utf-8"
    if path.suffix == ".json":
        return "application/json; charset=utf-8"
    guessed = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"{guessed}; charset=utf-8" if guessed.startswith("text/") else guessed


def deploy_site(uploader: SpacesUploader, public_dir: Path, include_data: bool) -> list[str]:
    """Envia a página (index.html, css, js). Os JSON de exemplo só vão com
    include_data, para um deploy da página nunca sobrescrever o timeline real."""
    sent = []
    for path in sorted(public_dir.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        rel = path.relative_to(public_dir).as_posix()
        if rel in ("timeline.json", "ads.json") and not include_data:
            continue
        uploader.put(rel, path.read_bytes(), content_type_of(path))
        sent.append(rel)
    return sent
