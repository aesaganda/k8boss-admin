"""
Project endpoints (§17).

Thin, like every router here. The read delegates to
:mod:`app.services.projects`; the plan and the write delegate to
:mod:`app.admin.projects`, which reaches a cluster only through the generic
apply path and therefore :func:`app.admin.mutate.mutate`. Nothing here builds
an object and nothing here decides whether a write is allowed.

The gate is echoed onto the plan rather than fetched separately, for §16's
reason: a dialog that had to ask a second endpoint whether creating is
permitted would render its button before knowing.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Path
from pydantic import BaseModel, ConfigDict, Field

from app.admin import projects as projects_admin
from app.services import projects as projects_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["projects"])


class PodSecurityRequest(BaseModel):
    """The three Pod Security admission modes, each optional."""

    model_config = ConfigDict(populate_by_name=True)

    enforce: str | None = None
    enforceVersion: str | None = None  # noqa: N815
    audit: str | None = None
    auditVersion: str | None = None  # noqa: N815
    warn: str | None = None
    warnVersion: str | None = None  # noqa: N815


class LimitItemRequest(BaseModel):
    """One LimitRange item. Every map is resource name to quantity string."""

    model_config = ConfigDict(populate_by_name=True)

    type: str
    max: dict[str, str] | None = None
    min: dict[str, str] | None = None
    default: dict[str, str] | None = None
    defaultRequest: dict[str, str] | None = None  # noqa: N815
    maxLimitRequestRatio: dict[str, str] | None = None  # noqa: N815


class SubjectRequest(BaseModel):
    kind: str
    name: str = Field(..., min_length=1, max_length=253)
    namespace: str | None = Field(None, max_length=253)


class ProjectRequest(BaseModel):
    """A project request — the OpenShift project-request template, as a body."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=63)
    displayName: str | None = Field(None, max_length=253)  # noqa: N815
    description: str | None = Field(None, max_length=2000)
    podSecurity: PodSecurityRequest | None = None  # noqa: N815
    quota: dict[str, str] | None = Field(
        None,
        description=(
            "ResourceQuota spec.hard, keyed by the quota resource name the API "
            "server uses: requests.cpu, limits.memory, pods, count/deployments.apps…"
        ),
    )
    limits: list[LimitItemRequest] | None = None
    admins: list[SubjectRequest] | None = None
    adminRole: Literal["admin", "edit", "view"] = "admin"  # noqa: N815
    isolateIngress: bool = False  # noqa: N815


class ProjectWriteRequest(ProjectRequest):
    """The plan's body plus the two fields that make it a write."""

    acknowledgeConsequences: list[str] = Field(  # noqa: N815
        default_factory=list,
        description=(
            "Every consequence code the plan returned. Named rather than a "
            "boolean: a caller that acknowledged one set and then changed the "
            "request has to read the new one."
        ),
    )
    dry_run: bool = Field(
        True,
        alias="dryRun",
        description=(
            "Project the Namespace through the API server, render the objects "
            "inside it, and write nothing."
        ),
    )


@router.get("/projects/{name}")
def get_project(
    name: str = Path(..., min_length=1, max_length=63, description="The namespace."),
) -> dict[str, Any]:
    """§17 — one namespace with its quota usage, limits, security posture,
    bindings and policies. A live read; every secondary is collected."""
    return projects_service.get_project(name)


@router.post("/projects/plan")
def get_project_plan(request: ProjectRequest) -> dict[str, Any]:
    """§17 — the objects a project would create, and what creating them means.

    Ungated, like §14's and §16's plans: everything it does is a read, and an
    operator on a read-only console has to be able to see what enabling writes
    would let it create. Writes nothing and records no audit row.
    """
    return projects_admin.plan(request.model_dump(by_alias=True))


@router.post("/projects")
def create_project(request: ProjectWriteRequest) -> dict[str, Any]:
    """§17 — create the namespace and what governs it, object by object.

    ``created: true`` means every object landed on a real write. A dry run
    never reports it, and a partial create reports which object failed and
    what grant it needed.
    """
    return projects_admin.create(
        request.model_dump(by_alias=True, exclude={"acknowledgeConsequences", "dry_run"}),
        dry_run=request.dry_run,
        acknowledge_consequences=request.acknowledgeConsequences,
    )


__all__ = ["router"]
