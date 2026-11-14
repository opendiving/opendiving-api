"""One permission vocabulary for API keys, OAuth and delegated content operations."""

DOMAINS = (
    "dives",
    "drafts",
    "sites",
    "trips",
    "gear",
    "people",
    "contacts",
    "courses",
    "certifications",
    "tags",
    "checkin",
    "presets",
)
SCOPES = frozenset(f"{domain}:{action}" for domain in DOMAINS for action in ("read", "write", "delete")) | {
    "profile:read",
    "profile:write",
    "preferences:read",
    "preferences:write",
    "files:read",
    "files:write",
    "export:read",
    "import:write",
    # Species writes reach past the account: a resolved species joins the catalog every account
    # shares, and a photo proposal shows the operator the proposer's name and note.
    "species:write",
}
DEFAULT_SCOPES = ["dives:read"]


def validate_scopes(scopes: list[str]) -> bool:
    return bool(scopes) and len(scopes) == len(set(scopes)) and set(scopes) <= SCOPES
