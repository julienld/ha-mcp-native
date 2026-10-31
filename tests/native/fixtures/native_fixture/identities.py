"""Retain disposable test users across a real Core process restart."""

import json
import secrets
from pathlib import Path
from typing import Any

from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER


async def fixture_users(hass: Any) -> dict[str, Any]:
    target = Path(hass.config.path("test-identities.json"))
    try:
        saved = json.loads(await hass.async_add_executor_job(target.read_text))
    except FileNotFoundError:
        saved = None
    if saved is not None:
        assert isinstance(saved, dict) and set(saved) == {"alpha", "beta", "reader"}
        for identity in saved.values():
            user = await hass.auth.async_get_user(identity["user_id"])
            assert user is not None and user.is_active
            assert isinstance(identity["password"], str) and identity["password"]
        return saved
    provider = hass.auth.get_auth_provider("homeassistant", None)
    users = {}
    for name, group in (
        ("alpha", GROUP_ID_ADMIN),
        ("beta", GROUP_ID_ADMIN),
        ("reader", GROUP_ID_USER),
    ):
        password = secrets.token_urlsafe(24)
        await provider.async_add_auth(name, password)
        credential = await provider.async_get_or_create_credentials({"username": name})
        user = await hass.auth.async_create_user(name, group_ids=[group])
        await hass.auth.async_link_user(user, credential)
        users[name] = {"username": name, "password": password, "user_id": user.id}
    return users
