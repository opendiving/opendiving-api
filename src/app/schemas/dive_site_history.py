from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field

from ..core.schemas import PublicUUIDSchema, StoredVocabulary


class DiveSiteHistoryDetail(BaseModel):
    """One shared member an entry changed, its values as the site's shared members read them."""

    field: Annotated[str, Field(description="The shared member's name")]
    old: Annotated[Any, Field(description="Its value before")]
    new: Annotated[Any, Field(description="Its value after")]


class DiveSiteHistoryEntryRead(PublicUUIDSchema):
    """One accepted change to a Commons record."""

    kind: Annotated[StoredVocabulary, Field(description="`added` or `changed`")]
    created_at: datetime
    made_by: Annotated[
        str | None,
        Field(
            description="The display name of whoever made the change: on a diver's entry the proposer's, where "
            "they chose to be shown; on a reviewer's correction the reviewer's. Null where none is shown"
        ),
    ]
    details: Annotated[
        list[DiveSiteHistoryDetail],
        Field(description="Each shared member that changed, the pin's two coordinates together; empty when added"),
    ]


class AdminDiveSiteHistoryEntryRead(DiveSiteHistoryEntryRead):
    """An entry as a reviewer reads it. On a reviewer's correction the three members it adds are null."""

    made_by: Annotated[
        str | None,
        Field(
            description="The display name of whoever made the change, the proposer's whatever they chose. Null "
            "once the account is deleted or asked to be"
        ),
    ]
    show_proposer: Annotated[
        bool | None, Field(description="Whether the diver chose to be shown; null once the proposal is gone")
    ]
    reviewed_by: Annotated[str | None, Field(description="The display name of who accepted the diver's change")]
    corrected: Annotated[
        bool | None, Field(description="Whether that review also wrote a correction; null on a correction itself")
    ]
