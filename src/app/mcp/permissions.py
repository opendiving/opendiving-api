"""One permission vocabulary for OAuth, tool execution and browser consent."""

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
    "checkin:share",
}
DEFAULT_SCOPES = ["dives:read"]


def validate_scopes(scopes: list[str]) -> bool:
    return bool(scopes) and len(scopes) == len(set(scopes)) and set(scopes) <= SCOPES
