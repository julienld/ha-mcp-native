"""Explicit GitHub/data fixtures, served over HTTP to unmodified HACS clients.

These are not public releases. Only the dummy integration is installable here.
Unknown routes fail closed; no proxy or fallback reaches the Internet.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import zipfile
from typing import Any

from aiohttp import web

REPOSITORY = "qualification/hacs-owner-fixture"
REPOSITORY_ID = "987654321"
DOMAIN = "qualification_dummy"
VERSIONS = ("3.0.0", "2.0.0", "1.0.0")
HACS_ID = "172733314"
STAMP = "2026-01-01T00:00:00Z"


def files(version: str) -> dict[str, bytes]:
    manifest = {
        "domain": DOMAIN,
        "name": "HACS owner qualification dummy",
        "version": version,
        "codeowners": [],
        "documentation": f"https://github.com/{REPOSITORY}",
        "iot_class": "local_push",
        "requirements": [],
    }
    return {
        "manifest.json": json.dumps(manifest).encode(),
        "__init__.py": (
            f'"""Disposable qualification integration {version}."""\n'
            f'VERSION = "{version}"\n'
            "async def async_setup(hass, config):\n"
            f"    hass.data['{DOMAIN}'] = VERSION\n"
            "    return True\n"
        ).encode(),
    }


def archive(version: str, *, corrupt: bool = False) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, content in files(version).items():
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            bundle.writestr(info, content)
    if not corrupt:
        return output.getvalue()
    # A CRC failure on the SECOND member proves HACS restores a partially
    # written tree, not merely that it handles a pre-download HTTP error.
    data = bytearray(output.getvalue())
    offset = data.index(files(version)["__init__.py"])
    data[offset] ^= 1
    return bytes(data)


def hacs_manifest() -> dict[str, Any]:
    return {
        "name": "HACS owner qualification dummy",
        "zip_release": True,
        "filename": "fixture.zip",
    }


def seed_repositories() -> dict[str, Any]:
    return {
        HACS_ID: {
            "full_name": "hacs/integration",
            "category": "integration",
            "domain": "hacs",
            "installed": True,
            "version_installed": "2.0.5",
            "last_version": "2.0.5",
            "default_branch": "main",
            "releases": True,
            "repository_manifest": {"name": "HACS"},
        }
    }


class ContentServer:
    """HTTP fixture with observable requests and a bounded lost-response gate."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.unexpected: list[str] = []
        self.fail_archive = False
        self.hold_archive = False
        self.archive_entered = asyncio.Event()
        self.release_archive = asyncio.Event()
        self.runner: web.AppRunner | None = None
        self.base = ""

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{host}/{path:.*}", self.respond)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{self.runner.addresses[0][1]}"

    async def close(self) -> None:
        self.release_archive.set()
        if self.runner:
            await self.runner.cleanup()

    async def respond(self, request: web.Request) -> web.Response:
        host, path = request.match_info["host"], "/" + request.match_info["path"]
        key = f"{request.method} {host}{path}"
        self.requests.append(key)
        headers = {
            "ETag": '"qualification-v1"',
            "X-RateLimit-Remaining": "5000",
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Reset": "4102444800",
        }
        if request.method != "GET":
            self.unexpected.append(key)
            return web.json_response({"message": "fixture GET only"}, status=405)
        if host == "data-v2.hacs.xyz" and path.endswith("/data.json"):
            section = path.split("/")[1]
            return web.json_response(
                [] if section in {"critical", "removed"} else {}, headers=headers
            )
        if host == "api.github.com" and path == "/rate_limit":
            rate = {"limit": 5000, "remaining": 5000, "reset": 4102444800, "used": 0}
            return web.json_response(
                {"resources": {"core": rate}, "rate": rate}, headers=headers
            )
        prefix = f"/repos/{REPOSITORY}"
        if host == "api.github.com" and (
            path == prefix or path.startswith(prefix + "/")
        ):
            payload = self.github(
                path[len(prefix) :], request.query.get("ref", VERSIONS[0])
            )
            if payload is not None:
                return web.json_response(payload, headers=headers)
        if host == "raw.githubusercontent.com":
            response = self.raw(path, headers)
            if response is not None:
                return response
        prefix = f"/{REPOSITORY}/releases/download/"
        if host == "github.com" and path.startswith(prefix):
            # The pinned owner passes its tags/<version> ref to the URL builder.
            ref = path.removeprefix(prefix).removeprefix("tags/")
            version, _, filename = ref.partition("/")
            if (version, filename) in {(tag, "fixture.zip") for tag in VERSIONS}:
                return await self.download(version, headers)
        self.unexpected.append(key)
        return web.json_response({"message": "unmodelled fixture route"}, status=404)

    async def download(self, version: str, headers: dict[str, str]) -> web.Response:
        self.archive_entered.set()
        if self.hold_archive:
            await asyncio.wait_for(self.release_archive.wait(), 30)
        return web.Response(
            body=archive(version, corrupt=self.fail_archive),
            content_type="application/zip",
            headers=headers,
        )

    @staticmethod
    def raw(path: str, headers: dict[str, str]) -> web.Response | None:
        if not path.startswith(f"/{REPOSITORY}/"):
            return None
        rest = path.removeprefix(f"/{REPOSITORY}/").removeprefix("tags/")
        version, _, filename = rest.partition("/")
        if version not in VERSIONS:
            return None
        if filename == "hacs.json":
            return web.json_response(hacs_manifest(), headers=headers)
        if filename.startswith(f"custom_components/{DOMAIN}/"):
            data = files(version).get(filename.rsplit("/", 1)[-1])
            if data is not None:
                return web.Response(body=data, headers=headers)
        return None

    @staticmethod
    def github(suffix: str, version: str) -> Any:
        version = version.removeprefix("tags/")
        if suffix == "":
            return {
                "id": int(REPOSITORY_ID),
                "name": REPOSITORY.split("/")[1],
                "full_name": REPOSITORY,
                "default_branch": "main",
                "archived": False,
                "description": "Explicit offline owner qualification fixture",
                "pushed_at": STAMP,
                "updated_at": STAMP,
                "created_at": STAMP,
                "stargazers_count": 0,
                "open_issues_count": 0,
                "topics": [],
                "owner": {"login": "qualification"},
            }
        if suffix == "/releases":
            return [
                {
                    "id": index + 1,
                    "tag_name": tag,
                    "name": tag,
                    "draft": False,
                    "prerelease": False,
                    "published_at": STAMP,
                    "created_at": STAMP,
                    "body": "Fixture release; never published to GitHub.",
                    "assets": [],
                }
                for index, tag in enumerate(VERSIONS)
            ]
        if suffix.startswith("/git/trees/"):
            names = ["hacs.json", f"custom_components/{DOMAIN}"] + [
                f"custom_components/{DOMAIN}/{name}" for name in files(VERSIONS[0])
            ]
            return {
                "sha": "a" * 40,
                "truncated": False,
                "tree": [
                    {
                        "path": name,
                        "type": "tree" if name.endswith(DOMAIN) else "blob",
                        "mode": "040000" if name.endswith(DOMAIN) else "100644",
                        "sha": "a" * 40,
                    }
                    for name in names
                ],
            }
        if suffix.startswith("/commits"):
            commit = {"sha": "a" * 40, "commit": {"committer": {"date": STAMP}}}
            return [commit] if suffix == "/commits" else commit
        if suffix == "/branches/main":
            return {"name": "main", "commit": {"sha": "a" * 40}}
        if suffix.startswith("/contents/") and version in VERSIONS:
            path = suffix.removeprefix("/contents/")
            content = (
                json.dumps(hacs_manifest()).encode()
                if path == "hacs.json"
                else files(version).get(
                    path.removeprefix(f"custom_components/{DOMAIN}/")
                )
            )
            if content is not None:
                return {
                    "name": path.rsplit("/", 1)[-1],
                    "path": path,
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(content).decode(),
                }
        return None
