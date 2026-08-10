from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# What the message is about. A closed vocabulary shared with the frontend's contact
# form (see `lib/validations/contact.ts` in opendiving-web) rather than free text, so
# the subject line the inbox sees is triageable at a glance. Every option maps to
# something this app actually does - don't add one for a channel that doesn't exist.
ContactCategory = Literal[
    "support",
    "bug",
    "feature",
    "import",
    "account",
    "privacy",
    "security",
    "other",
]

# Human-readable labels for the subject line of the forwarded email. Keeping them here,
# next to the vocabulary itself, means adding a category is a one-place change.
CONTACT_CATEGORY_LABELS: dict[str, str] = {
    "support": "Help using OpenDiving",
    "bug": "Bug report",
    "feature": "Feature request",
    "import": "Dive-computer import",
    "account": "Account & data",
    "privacy": "Privacy",
    "security": "Security",
    "other": "Other",
}


class ContactMessageRequest(BaseModel):
    """A message from the frontend's contact form.

    Deliberately unauthenticated - the people most likely to need this (someone locked
    out of their account, or a visitor who hasn't signed up) can't present a token.
    """

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=100, examples=["Jacques Cousteau"])]
    email: Annotated[EmailStr, Field(examples=["diver@example.com"])]
    category: ContactCategory = "support"
    subject: Annotated[str, Field(min_length=3, max_length=150, examples=["Suunto export won't import"])]
    # The upper bound is a spam/abuse guard, not an editorial one - it's several pages
    # of prose, far more than a support request needs.
    message: Annotated[str, Field(min_length=10, max_length=5000)]


class ContactMessageResponse(BaseModel):
    """Deliberately says nothing about the recipient inbox or whether delivery actually
    succeeded downstream - see `POST /contact`.
    """

    message: str = "Thanks - your message is on its way. We'll reply to the address you gave us."
