"""Explicit domain grants; no implicit superuser bypass."""

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    INTERNAL = "internal"
    EXTERNAL = "external"
    FULL = "full"


MANAGE_ADMINS = ("system", "manage_admins")
MANAGE_USERS = ("internal", "manage_users")
COMMON = ("read_feedback", "manage_reports", "view_statistics")


def role_grants(role: Role) -> set[tuple[str, str]]:
    role = Role(role)
    grants = set()
    if role in {Role.INTERNAL, Role.FULL}:
        grants |= {("internal", permission) for permission in (*COMMON, "manage_users")}
    if role in {Role.EXTERNAL, Role.FULL}:
        grants |= {("external", permission) for permission in (*COMMON, "manage_external_people")}
    return grants


class AccessDenied(Exception):
    """Safe rejection; does not confirm whether the target exists."""


class InputError(Exception):
    """User-correctable error with a fixed, safe message."""


@dataclass(frozen=True, repr=False)
class Principal:
    user_id: str
    telegram_id: int
    display_name: str
    admin_id: str | None
    grants: frozenset[tuple[str, str]]

    def allows(self, scope: str, permission: str) -> bool:
        return (scope, permission) in self.grants

    def require(self, scope: str, permission: str) -> None:
        if not self.allows(scope, permission):
            raise AccessDenied()
