# MCP architecture

The optional MCP server runs in the API process at `/api/v1/mcp`. Enable it with `MCP_ENABLED=true`.
Its canonical public origin is `FRONTEND_URL`, which must use HTTPS except on localhost. The install
bundle and the web app route both OAuth discovery documents to the API.

## Protocol and packages

The official `mcp` Python SDK supplies Streamable HTTP, MCP 2026-07-28 envelopes, legacy 2025-11-25
compatibility, resource-server authentication and OAuth authorization-code handlers. OpenDiving
supplies durable persistence, browser consent and application permissions. The resource server and
authorization server are separate ASGI applications in one process.

The SDK performs PKCE verification, client validation and exact redirect matching. Native loopback
IP callbacks may vary their port under RFC 8252; their host, path and query remain exact. The OAuth
boundary additionally requires a canonical RFC 8707 resource on authorization and token requests,
and explicit PKCE S256. Issuer and resource URLs never come from request headers.

`jsonschema` validates tool arguments against schemas derived from the existing API contract.
Existing Pydantic models remain authoritative for content validation. Authlib was evaluated, but its
Starlette integration is a client, not an authorization server. It is not required. FastMCP was
evaluated; the official SDK supplies the protocol pieces used here without another framework layer.
Inspector is a development tool and is not a runtime dependency.

References checked on 2026-11-01:

- <https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization>
- <https://modelcontextprotocol.io/specification/2026-07-28/basic/transports>
- <https://modelcontextprotocol.io/docs/2026-07-28/learn/versioning>
- <https://py.sdk.modelcontextprotocol.io/>
- <https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0>

## Delegated authorization

Each browser approval creates a connection owned by one account and one OAuth client. The user
selects requested permissions, with reads selected initially. Write and delete permissions are
separate for each content domain. Profile and preference permissions are independent. Check-in links
stay browser-only, like credential management. Import permission authorizes restoring content across
logbook domains.

Codes expire after two minutes and are consumed under a database lock. They bind the user, client,
resource, PKCE challenge and redirect. Pending consent expires after ten minutes and is single-use.
A browser session is required to approve or revoke a connection. API keys and MCP tokens cannot
authorize new connections.

Access tokens last fifteen minutes. Refresh tokens last thirty days and rotate under a lock on their
connection. Reusing a spent refresh token revokes that connection, including its successors.
PostgreSQL stores SHA-256 hashes, never the original codes or bearer secrets. Every request checks
the live connection and account, so revocation and account deletion take effect immediately.
Delegated principals always have `is_superuser=false`.

HTTPS client metadata documents are supported. Fetches resolve DNS once, reject non-public
addresses, connect to a checked address, and verify TLS for the original hostname. They neither
follow redirects nor consult environment proxy settings, and responses are capped at 32 KiB.
Documents are cached for one hour. Dynamic public-client registration is also supported for
compatibility. No client secrets are stored.

## Content boundary

`catalog.py` is a reviewed, explicit list of operations and required permissions. Adding a REST
route never exposes it through MCP automatically. Credential management, sign-in identity changes,
check-in links, account deletion and administration are absent. Scope filtering affects both tool
listing and execution.

The catalog explicitly distinguishes JSON results from file downloads. File cardinality and
retry-key support come from each route's OpenAPI contract. A single-file operation accepts one
handle; logbook imports accept multiple. Only routes declaring `Idempotency-Key` expose a retry key.

The dispatcher makes an in-process ASGI request to the selected existing content operation. This
retains ownership checks before cache reads, cache invalidation, quotas, import validation,
idempotency dependencies and response serialization. It avoids duplicating the route-heavy
application logic across two sets of handlers. A request-scoped Python capability identifies the
delegated actor. It is never an HTTP header, never a browser credential, and never a global
dependency override. Clients cannot choose URLs, methods or arbitrary headers. Download-ticket query
parameters are excluded.

Profile and preference views share explicit read and write field policies. Updates expose disjoint
subsets of the existing update schema, and reads apply their view's allowlist. Operations requiring
multiple capabilities list each scope, such as merging dives, attaching files and publishing drafts.
The catalog's schema tests hold these operations to the live API contract.

The shared `authorization` package owns this reviewed operation catalog, scope validation and
account field policies for MCP and scoped API keys. The normal REST authentication boundary checks
the same policy before handlers or cached reads run, including internal requests carrying an MCP
actor's scopes. API-key secrets, expiry and revocation remain separate from OAuth consent, codes and
token rotation. Legacy API keys keep their existing read/write behavior.

## Files and operation records

Uploads use expiring handles private to the account and connection. The HTTP staging endpoint
accepts multipart uploads of at most 10 MiB, with an account-wide 50 MiB temporary quota enforced
under an account lock. Files live in the existing blob store. The blob-key inventory includes the
temporary-file table so account purge and orphan sweeping recognize them. Staging reclaims expired
and revoked connections' files under the same account lock, so their quota is available immediately.

Upload tools accept staged handles and retain existing preview/apply import behavior. Binary results
become private download handles. The `opendiving://file/{handle}` resource describes a download
without embedding large binary data in model context. Download requests use the connection's bearer
token. Downloads preserve the attachment filename and media type. They spill to disk after 1 MiB,
stop at the 50 MiB temporary-file limit, and copy into either blob backend without materializing the
spool. A known oversized content length is refused before capturing body chunks. JSON tool results
are capped at 512 KiB.

The connection activity endpoint records tool names, status codes and timestamps, without arguments
or content. An hourly worker task removes expired credentials and temporary files, unreferenced
client registrations, and audit events older than ninety days.

## Verification

HTTP integration tests use migrated PostgreSQL and the real OAuth handlers. They cover PKCE,
resource and redirect binding, code consumption races, refresh replay, revocation, scope checks and
two-account ownership. Official SDK clients exercise modern discovery and legacy initialization
through HTTP transport. Repository checks cover authentication, API descriptions, blob ownership,
migration drift, types and formatting.
