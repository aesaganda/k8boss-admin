"""
The request-body pieces every write endpoint shares.

There is one of them, and it carries one field, and that field is the reason
this module exists rather than the base class being copied into each router —
which is what it was, in three private ``_MutationBody`` classes and five
inline declarations, one of whose docstrings read "See §6's copy for why".

``dryRun`` defaults to **true** and its alias accepts ``dry_run`` as well. Both
halves are safety rather than convenience:

* A client that **forgets** the field gets a projection, not a write. §0.3 says
  every mutation can be dry-run and the destructive ones must be; a default of
  false would make the whole contract opt-in, and the endpoint that forgot to
  opt in would be discovered by an operator, on a cluster.
* A client that sends the **snake_case** spelling is understood rather than
  silently falling back to the default. For this one field that fallback is not
  a shrug: an operator asking for a real drain would receive a dry run and be
  told ``applied: false`` about a node they believe is now empty. It is the one
  misunderstanding in this API that costs somebody an incident.

Both properties held in every copy. That is exactly the problem: eight
independent chances to stop holding, in eight files, with nothing that would
fail if the ninth got it wrong — and a default flipped in one of them looks
identical from outside until a cluster changes.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MutationBody(BaseModel):
    """Base for every §1.5 write body: ``dryRun``, defaulting to true.

    Subclass it and add the fields the write needs. Do not redeclare
    ``dry_run`` — a subclass that overrides it takes the default with it, and
    the point of this class is that the default is decided once.
    """

    model_config = ConfigDict(populate_by_name=True)

    dry_run: bool = Field(
        True,
        alias="dryRun",
        description="Send dryRun=All to the API server and return the projected diff.",
    )


__all__ = ["MutationBody"]
