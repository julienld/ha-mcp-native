"""Owner-specific update instructions; no client configuration or secret values."""

from typing import Any


def installation_guides() -> dict[str, Any]:
    return {
        method: update_guide(method)
        for method in (
            "component",
            "app",
            "docker",
            "pip",
            "uv",
            "uvx",
            "pipx",
            "source",
        )
    }


def update_guide(method: str, distribution: str | None = None) -> dict[str, Any]:
    package = distribution if distribution in {"ha-mcp", "ha-mcp-dev"} else "ha-mcp"
    paths = {
        "component": [
            "Use the ha_mcp_tools update in HACS or Marketplace; keep the existing server entry and its runtime selection.",
            "After files are installed, restart Home Assistant using ha_restart and reconnect; verify running and installed versions again.",
        ],
        "app": [
            "Open Settings > Apps (add-ons) > the running HA-MCP app > Update. Keep the current stable/development flavor and options.",
            "Supervisor stops and replaces this server. Reconnect after the app starts and check its version in Supervisor.",
        ],
        "docker": [
            "On the Docker host, use the existing Compose project or container deployment to pull its configured HA-MCP image and recreate that service.",
            "Keep the image channel, mounts, environment, ports and restart policy. Record the previous image digest before replacing it; verify the new container is healthy.",
        ],
        "pip": [
            f"On the server host, stop the server and run the environment's Python with -m pip install --upgrade {package}; restart the same command."
        ],
        "uv": [
            f"On the server host, use the owning uv project/lock or uv tool upgrade {package}, as applicable; restart the same entrypoint.",
            "A cached uv environment alone cannot distinguish uvx from uv run. Check the existing launcher and use the uvx guide when applicable.",
        ],
        "uvx": [
            f"End the current server process, then relaunch its existing uvx command with --refresh and the same {package} version/channel policy."
        ],
        "pipx": [
            f"On the server host, run pipx upgrade {package}, then restart the existing server command."
        ],
        "source": [
            "On the checkout host, save local work and record the current revision/lock. Fetch and review the intended release, update the checkout and synchronize its locked environment, then restart the same entrypoint.",
        ],
        "unknown": [
            "Identify the process's installation owner before changing files. Do not infer the server host from the Home Assistant connection."
        ],
    }
    return {
        "steps": paths.get(method, paths["unknown"]),
        "preserve": [
            "runtime",
            "distribution/channel",
            "authentication",
            "connection URL",
            "stored data",
            "client configuration",
        ],
        "breaking_changes": "Review changes to URL, authentication, runtime, distribution or data with the user before applying them. An unchanged URL alone is not a compatibility assessment.",
        "rollback": "Keep a pre-update backup and previous artifact/revision. Restore through the installation owner while stopped; reconcile owner metadata and verify the running version. A code-only snapshot does not restore dependencies or data migrations.",
        "migration": "Runtime migration is optional and separate from an ordinary update. Legacy mode is not currently deprecated; do not switch it during an ordinary update.",
        "documentation": "https://homeassistant-ai.github.io/ha-mcp/setup/",
    }
