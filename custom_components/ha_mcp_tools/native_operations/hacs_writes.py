"""HACS mutations use their installed repository owners, never WS handlers."""

import asyncio
import copy
import inspect
from importlib import import_module
from typing import Any

from ha_mcp.runtime.hacs_results import (
    added_data,
    download_data,
    refresh_data,
    remove_data,
)
from ha_mcp.runtime.hacs_values import CATEGORY_MAP
from ha_mcp.runtime.validation import validate_identifier_not_empty
from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .hacs_access import HacsAccess, check_self_files, repository_image
from .hacs_reads import HacsReads
from .hacs_updates import call_install_service, install_service, pin_install_service

_METHODS = {
    "download": "async_download_repository",
    "remove": "uninstall",
    "update_information": "update_repository",
}


class NativeHacs(HacsReads):
    def __init__(self, manager: Any, actor: Any) -> None:
        self.manager = manager
        self.actor = actor

    async def _hacs_download(
        self, repository_id: str | None, version: str | None
    ) -> dict[str, Any]:
        return await self._repository_write("download", repository_id, version)

    async def _hacs_remove(self, repository_id: str | None) -> dict[str, Any]:
        return await self._repository_write("remove", repository_id, None)

    async def _hacs_update_information(
        self, repository_id: str | None
    ) -> dict[str, Any]:
        return await self._repository_write("update_information", repository_id, None)

    async def _repository_write(
        self, action: str, identifier: str | None, version: str | None
    ) -> dict[str, Any]:
        identifier = validate_identifier_not_empty(identifier, "repository_id")
        async with registry_update_lock("hacs", "repositories"):
            access = await self.access(writes=True)
            repo = access.find(identifier)
            access.guard(action, repo)
            if action == "remove" and not repo.data.installed:
                raise ToolFailure(
                    "RESOURCE_NOT_FOUND",
                    "Repository is not downloaded; nothing to remove",
                    status="not_started",
                )
            self_affecting = (
                action in {"download", "remove"}
                and repo.data.category == "integration"
                and repo.data.domain in {"ha_mcp_tools", "hacs"}
            )
            prepared = (
                install_service(access, repo, version) if action == "download" else None
            )
            command = access.command(action)
            proposal = {"repository": str(repo.data.id)}
            if action == "download" and version:
                proposal["version"] = version
            payload, registration = (
                access.validate(command, proposal)
                if prepared is None
                else (proposal, None)
            )
            method_name = (
                "async_install_repository"
                if action == "download" and access.domain == "marketplace"
                else _METHODS[action]
            )
            method = getattr(repo, method_name, None)
            parameters = (
                {"ref": payload.get("version")}
                if action == "download"
                else (
                    {"ignore_issues": True, "force": True}
                    if action == "update_information"
                    else {}
                )
            )
            if prepared is None:
                if not callable(method):
                    raise ToolFailure(
                        "UNSUPPORTED_OPERATION",
                        "The loaded store lacks the required repository operation",
                        status="not_started",
                    )
                try:
                    inspect.signature(method).bind(**parameters)
                except (TypeError, ValueError) as error:
                    raise ToolFailure(
                        "UNSUPPORTED_OPERATION",
                        "The loaded store repository method has an unqualified signature",
                        status="not_started",
                    ) from error
            before = repository_image(repo)
            return await await_owned_io(
                self._commit(
                    access,
                    repo,
                    before,
                    action,
                    method,
                    parameters,
                    registration,
                    self_affecting,
                    prepared,
                )
            )

    async def _commit(
        self,
        access: HacsAccess,
        repo: Any,
        before: dict[str, Any],
        action: str,
        method: Any,
        parameters: dict[str, Any],
        registration: Any,
        self_affecting: bool,
        prepared: Any = None,
    ) -> dict[str, Any]:
        if (
            prepared is None
            and access.domain == "hacs"
            and action in {"download", "remove"}
        ):
            check_self_files(access.owner, repo, "ha_mcp_tools")
        await self.manager.visibility.before(
            "ha_manage_hacs",
            {
                "action": action,
                "repository_id": str(before["data"]["id"]),
                "version": parameters.get("ref"),
            },
            self.actor,
        )
        # Visibility can await file/policy reads. Recheck every live actor,
        # owner, image, schema and method only after that final await.
        await access.admit(repo, before)
        access.guard(action, repo)
        if registration is not None:
            access.pin_validator(access.command(action), registration)
        method_name = (
            "async_install_repository"
            if action == "download" and access.domain == "marketplace"
            else _METHODS[action]
        )
        if prepared is None and getattr(repo, method_name) != method:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "HACS repository operation changed",
                status="not_started",
            )
        if prepared is not None:
            pin_install_service(access, repo, prepared)
        # Everything below this point can mutate HACS metadata, Core registries
        # or files. A provider exception/cancellation never invites replay.
        try:
            if prepared is not None:
                await call_install_service(access, prepared)
                pin_install_service(access, repo, prepared)
            else:
                await self._direct_dispatch(
                    access, repo, before, action, method, parameters
                )
            access.pin(repo, writes=True)
            self._verify_action(repo, action, parameters.get("ref"))
            after = repository_image(repo)
            if (
                access.domain == "hacs"
                and action == "download"
                and not before["data"].get("installed")
            ):
                await self._new_install(access, repo, after)
            await access.admit(repo, after)
            if registration is not None:
                access.pin_validator(access.command(action), registration)
            await access.storage.async_write()
            await access.observe(repo, after)
            if action == "update_information":
                await self._refresh_entities(access, repo, after)
            return self._write_result(access, repo, action, parameters, self_affecting)
        except (Exception, asyncio.CancelledError) as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                f"The store operation was dispatched and may have changed metadata or files. Inspect the store before another change; no retry was performed. Provider: {error}",
                status="outcome_unknown",
            ) from error

    async def _direct_dispatch(
        self,
        access: HacsAccess,
        repo: Any,
        before: dict[str, Any],
        action: str,
        method: Any,
        parameters: dict[str, Any],
    ) -> None:
        try:
            await method(**parameters)
        finally:
            # Core adds the update entity even when a post-install step fails.
            if (
                access.domain == "marketplace"
                and action == "download"
                and not before["data"].get("installed")
                and repo.data.installed
            ):
                access.pin(repo, writes=True)
                await self._new_install(access, repo, repository_image(repo))

    @staticmethod
    def _verify_action(repo: Any, action: str, version: str | None) -> None:
        if action == "download" and (
            not repo.data.installed
            or (
                version
                and version
                not in (repo.data.default_branch, repo.data.installed_version)
            )
        ):
            raise RuntimeError(
                "The store did not confirm the requested installed version"
            )
        if action == "remove" and repo.data.installed:
            raise RuntimeError("The store did not confirm removal")

    async def _new_install(
        self, access: HacsAccess, repo: Any, after: dict[str, Any]
    ) -> None:
        if access.domain == "marketplace":
            from homeassistant.helpers.dispatcher import async_dispatcher_send

            signal = import_module(
                "homeassistant.components.marketplace.const"
            ).SIGNAL_REPOSITORY_INSTALLED
            await access.admit(repo, after)
            async_dispatcher_send(access.hass, signal, repo)
            return
        HacsDispatchEvent = import_module(
            "custom_components.hacs.enums"
        ).HacsDispatchEvent

        await access.admit(repo, after)
        access.owner.async_dispatch(HacsDispatchEvent.RELOAD, {"force": True})
        await access.admit(repo, after)
        await access.owner.async_recreate_entities()
        access.pin(repo, after, writes=True)

    async def _refresh_entities(
        self, access: HacsAccess, repo: Any, after: dict[str, Any]
    ) -> None:
        coordinator = access.owner.coordinators.get(repo.data.category)
        if coordinator is None:
            raise RuntimeError(
                "HACS repository coordinator is unavailable after refresh"
            )
        await access.admit(repo, after)
        if access.owner.coordinators.get(repo.data.category) is not coordinator:
            raise RuntimeError("HACS repository coordinator changed")
        coordinator.async_update_listeners()
        access.pin(repo, after, writes=True)

    def _write_result(
        self,
        access: HacsAccess,
        repo: Any,
        action: str,
        parameters: dict[str, Any],
        self_affecting: bool,
    ) -> dict[str, Any]:
        identifier, name = str(repo.data.id), repo.data.full_name
        if action == "download":
            data = download_data(identifier, name, parameters.get("ref"), {})
            data["message"] = f"HACS downloaded the repository files for {name}"
        elif action == "remove":
            data = remove_data(identifier, name, {})
        else:
            data = refresh_data(identifier, name, {})
        data.update(
            installed_version=repo.display_installed_version,
            pending_restart=repo.pending_restart,
            self_affecting=self_affecting,
            restart_requested=False,
            config_entries_removed=False,
            store=access.domain,
            **(
                {"hacs_version": access.version}
                if access.domain == "hacs"
                else {"core_version": access.version}
            ),
        )
        if self_affecting:
            data.update(
                runtime_still_loaded=True,
                active_version_verified=False,
                note="HACS changed the on-disk files; the existing runtime remains loaded. No restart or config-entry removal was requested.",
            )
        if access.domain == "marketplace":
            data["message"] = data["message"].replace("HACS", "Marketplace")
            data["note"] = data.get("note", "").replace("HACS", "Marketplace")
        return self.wrapped(data)

    async def _hacs_add_repository(
        self, repository: str, category: str
    ) -> dict[str, Any]:
        if "/" not in repository:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "Invalid repository format. Must be 'owner/repo'",
                status="not_started",
            )
        async with registry_update_lock("hacs", "repositories"):
            access = await self.access(writes=True)
            access.category(category)
            access.guard("add_repository")
            extract_repository_from_url = import_module(
                f"{access.module_root}.utils.regex"
            ).extract_repository_from_url

            payload, registration = access.validate(
                access.command("add_repository"),
                {
                    "repository": repository,
                    "category": CATEGORY_MAP.get(category, category),
                },
            )
            name = extract_repository_from_url(payload["repository"])
            if (
                name is None
                or payload["category"] not in access.owner.common.categories
            ):
                raise ToolFailure(
                    "VALIDATION_INVALID_PARAMETER",
                    "Repository or category is not supported by the installed HACS",
                    status="not_started",
                )
            renamed = access.owner.common.renamed_repositories.get(name, name)
            if renamed != name:
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    f"The store redirects {name} to {renamed}; request that repository explicitly to preserve provenance.",
                    status="not_started",
                )
            existing = access.repositories.get_by_full_name(name)
            if existing is not None:
                if existing.data.category != payload["category"]:
                    raise ToolFailure(
                        "CONFIG_CONFLICT",
                        "Repository is already registered under another category",
                        status="not_started",
                    )
                return self.wrapped(
                    {
                        **added_data(
                            repository,
                            category,
                            access.project(existing),
                        ),
                        "already_registered": True,
                    }
                )
            before = {
                "skip": sorted(access.owner.common.skip),
                "renamed": copy.deepcopy(access.owner.common.renamed_repositories),
                "categories": sorted(access.owner.common.categories),
            }
            registrar = access.owner.async_register_repository
            inspect.signature(registrar).bind(
                repository_full_name=name, category=payload["category"]
            )
            return await await_owned_io(
                self._register(
                    access,
                    name,
                    category,
                    payload["category"],
                    before,
                    registration,
                    registrar,
                )
            )

    async def _register(
        self,
        access: HacsAccess,
        name: str,
        category: str,
        native_category: str,
        before: dict[str, Any],
        registration: Any,
        registrar: Any,
    ) -> dict[str, Any]:
        await self.manager.visibility.before(
            "ha_manage_hacs",
            {"action": "add_repository", "repository": name, "category": category},
            self.actor,
        )
        await access.admit()
        access.guard("add_repository")
        access.pin_validator(access.command("add_repository"), registration)
        if access.owner.async_register_repository != registrar:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "HACS registration owner changed",
                status="not_started",
            )
        if access.repositories.get_by_full_name(
            name
        ) is not None or not json_values_equal(
            {
                "skip": sorted(access.owner.common.skip),
                "renamed": access.owner.common.renamed_repositories,
                "categories": sorted(access.owner.common.categories),
            },
            before,
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "HACS repository registration changed during preparation",
                status="not_started",
            )
        try:
            access.owner.common.skip.discard(name)
            # Await the real registration owner. No detached job, signal waiter,
            # polling, timeout retry or fake success acknowledgment is required.
            errors = await registrar(
                repository_full_name=name, category=native_category
            )
            access.pin(writes=True)
            if errors:
                raise RuntimeError("; ".join(str(error) for error in errors))
            repo = access.repositories.get_by_full_name(name)
            if repo is None or repo.data.category != native_category:
                raise RuntimeError("HACS did not register the requested repository")
            access.remember(repo)
            after = repository_image(repo)
            await access.admit(repo, after)
            await access.storage.async_write()
            await access.observe(repo, after)
            return self.wrapped(
                {
                    **added_data(name, category, access.project(repo)),
                    "store": access.domain,
                }
            )
        except (Exception, asyncio.CancelledError) as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                f"Store registration was dispatched but its final state is uncertain. Inspect its repository list before retrying. Provider: {error}",
                status="outcome_unknown",
            ) from error
