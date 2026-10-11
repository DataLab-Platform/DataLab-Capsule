# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""HTTP interface of the capsule registry (``registry`` extra: FastAPI, uvicorn).

Routes (``/api/v1``), described by the OpenAPI document at ``/openapi.json``:

- ``GET /status``: registry name and library version;
- ``GET /revisions``: flat list of revisions, most recent first;
- ``POST /revisions``: publish a manifest (JSON body);
- ``GET /revisions/{fingerprint}``: the manifest, as published;
- ``GET /revisions/{fingerprint}/reviews``: its reviews;
- ``PUT /revisions/{fingerprint}/attachments/{name}``: upload a declared
  attachment (raw body);
- ``GET /revisions/{fingerprint}/attachments/{name}``: download it, always as
  ``application/octet-stream`` with ``Content-Disposition: attachment``, so
  that a browser never renders or runs it;
- ``POST /reviews``: publish a review of a published revision;
- ``GET /reviews/{fingerprint}``: one review.

Reading is open. Writing needs ``Authorization: Bearer <token>`` when the
registry has a token. Cross-origin requests are allowed from the configured
origins, without credentials, so that DataLab-Web can use the registry.
"""

from __future__ import annotations

import argparse
import hmac
import os
import sys
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from datalab_capsule import __version__
from datalab_capsule.registry import (
    MAX_LISTED,
    MAX_REVIEW_BYTES,
    Registry,
    RegistryError,
    RegistryNotFound,
)

__all__ = ["create_app", "main"]

_JSON = "application/json"


async def _body(request: Request, limit: int) -> bytes:
    """Read the request body, refusing it as soon as it exceeds *limit*."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"The body exceeds {limit} bytes")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"The body exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(
    registry: Registry,
    *,
    token: str | None = None,
    origins: Sequence[str] = ("*",),
) -> FastAPI:
    """Return the registry application.

    Args:
        registry: The registry served.
        token: Bearer token required to publish; None leaves publishing open
         (local use only).
        origins: Origins allowed to call the registry from a browser.
    """
    app = FastAPI(
        title="DataLab capsule registry",
        version=__version__,
        description=(
            "Publish and retrieve capsule manifests, their declared attachments "
            "and their reviews. Workspaces are never stored."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(RegistryError)
    async def _refused(_request: Request, exc: RegistryError) -> JSONResponse:
        status = 404 if isinstance(exc, RegistryNotFound) else 422
        return JSONResponse({"detail": str(exc)}, status_code=status)

    def _authorize(request: Request) -> None:
        if token is None:
            return
        given = request.headers.get("authorization", "")
        if not hmac.compare_digest(given.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "A valid bearer token is required to publish")

    @app.get("/api/v1/status")
    def status() -> dict[str, Any]:
        """Name and version of the registry; whether publishing needs a token."""
        return {
            "registry": "datalab-capsule",
            "version": __version__,
            "token_required": token is not None,
        }

    @app.get("/api/v1/revisions")
    def list_revisions(limit: int = MAX_LISTED) -> list[dict[str, Any]]:
        """Flat list of revisions, most recently published first."""
        return registry.list_revisions(limit)

    @app.post("/api/v1/revisions", status_code=201)
    async def publish_manifest(request: Request) -> JSONResponse:
        """Publish a capsule manifest; publishing it again changes nothing."""
        _authorize(request)
        result = registry.publish_manifest(
            await _body(request, registry.max_manifest_bytes)
        )
        return JSONResponse(result, status_code=201 if result["created"] else 200)

    @app.get("/api/v1/revisions/{fingerprint}")
    def get_manifest(fingerprint: str) -> Response:
        """The manifest of a revision, as published."""
        return Response(registry.get_manifest(fingerprint), media_type=_JSON)

    @app.get("/api/v1/revisions/{fingerprint}/reviews")
    def list_reviews(fingerprint: str) -> list[dict[str, Any]]:
        """Reviews of a revision, oldest first."""
        return registry.list_reviews(fingerprint)

    @app.put("/api/v1/revisions/{fingerprint}/attachments/{name}", status_code=204)
    async def put_attachment(fingerprint: str, name: str, request: Request) -> None:
        """Upload an attachment declared by the manifest (size and SHA-256)."""
        _authorize(request)
        registry.put_attachment(
            fingerprint, name, await _body(request, registry.max_attachment_bytes)
        )

    @app.get("/api/v1/revisions/{fingerprint}/attachments/{name}")
    def get_attachment(fingerprint: str, name: str) -> Response:
        """Download an attachment; it is never served for display."""
        return Response(
            registry.get_attachment(fingerprint, name),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/v1/reviews", status_code=201)
    async def publish_review(request: Request) -> JSONResponse:
        """Publish a review of a published revision."""
        _authorize(request)
        result = registry.publish_review(await _body(request, MAX_REVIEW_BYTES))
        return JSONResponse(result, status_code=201 if result["created"] else 200)

    @app.get("/api/v1/reviews/{fingerprint}")
    def get_review(fingerprint: str) -> Response:
        """One review, as published."""
        return Response(registry.get_review(fingerprint), media_type=_JSON)

    return app


def main(argv: Sequence[str] | None = None) -> int:
    """Run a registry server (``workspace-capsule-registry``)."""
    parser = argparse.ArgumentParser(
        prog="workspace-capsule-registry",
        description="Serve a capsule registry (manifests, attachments, reviews).",
    )
    parser.add_argument("root", help="registry directory (created if needed)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--token-env",
        metavar="VARIABLE",
        help="environment variable holding the bearer token required to publish",
    )
    parser.add_argument(
        "--origin",
        action="append",
        dest="origins",
        help="origin allowed from a browser (repeat; default: any)",
    )
    args = parser.parse_args(argv)
    token = None
    if args.token_env:
        token = os.environ.get(args.token_env)
        if not token:
            print(f"Environment variable {args.token_env} is empty", file=sys.stderr)
            return 1
    import uvicorn  # pylint: disable=import-outside-toplevel

    app = create_app(Registry(args.root), token=token, origins=args.origins or ("*",))
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
