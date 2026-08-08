# Open Diving API

Backend API for Open Diving app.

## Authentication

There is a single entry point into the app: email magic link or Google - no
passwords, no separate sign up flow. See `src/app/api/v1/auth.py` for the endpoints
(`/auth/email/request`, `/auth/email/verify`, `/auth/google`, `/auth/complete`) and
`DECISIONS.md` for the full design rationale.

Changing an account's email (`src/app/api/v1/users.py`) reuses the same magic-link
mechanics: `POST /user/email-change/request` emails a confirmation link to the
*new* address, and the change only applies once `POST /user/email-change/verify`
confirms it - see `DECISIONS.md`.

All endpoints below are mounted under `/api/v1` (e.g. `/api/v1/auth/email/request`).

### User journeys

Every journey funnels through the same question, answered once by
`services.auth_service.resolve_identity`: "has this verified identity (an email
address, or - for Google - a stable provider subject id) been seen before?" The
answer decides whether the caller is signed in immediately or sent to onboarding -
there is no other branch point, and no `User` row is ever created outside of
`POST /auth/complete`.

```mermaid
flowchart TD
    A[Verified identity: email or Google] --> B{Provider id already\nlinked to an account?}
    B -->|Yes - Google, seen before| C[Sign in as that account]
    B -->|No| D{Account exists\nfor this email?}
    D -->|Yes| E[Link this provider to it\nif not linked yet]
    E --> C
    D -->|No| F[No account yet]
    F --> G[Issue onboarding session\nemail + provider + optional name/avatar]
    G --> H[POST /auth/complete\nname + username]
    H --> I[Create User + AuthenticationProvider\nin one transaction]
    I --> C
```

#### 1. Sign up with email (new user)

No separate "register" endpoint - a brand-new email just falls out of the same
`/auth/email/request` → `/auth/email/verify` pair as signing in, because the server
doesn't know yet whether the address belongs to anyone.

```mermaid
sequenceDiagram
    participant U as User
    participant FE as Frontend
    participant API as API
    participant DB as Database
    participant Mail as Resend

    U->>FE: Enters email, clicks Continue
    FE->>API: POST /auth/email/request {email}
    API->>DB: Invalidate any previous live\nsign_in request for this email
    API->>DB: Create AuthenticationRequest\n(token_hash, expires_at, purpose=sign_in)
    API->>Mail: Send magic link email
    API-->>FE: "Check your email for the next step."
    Note over API,FE: Same generic response whether\nor not the email has an account

    U->>Mail: Opens email, clicks link
    Mail->>FE: GET /auth/verify?token=...
    FE->>API: GET /auth/email/verify/check?token=...
    API-->>FE: valid=true, email
    FE-->>U: Shows "Sign in as {email}" button

    U->>FE: Clicks "Sign in"
    FE->>API: POST /auth/email/verify {token}
    API->>DB: Validate token, mark used_at
    API->>DB: resolve_identity(email) -> no account
    API-->>FE: status=onboarding_required\n+ onboarding_token, email
    FE->>U: Redirect to /onboarding

    U->>FE: Enters name + username
    FE->>API: POST /auth/complete\n{onboarding_token, name, username}
    API->>DB: Create User + AuthenticationProvider\n(single transaction)
    API-->>FE: status=authenticated + access_token\n(+ refresh_token cookie)
    FE->>U: Redirect to /dashboard
```

#### 2. Sign in with email (existing user)

Identical first step to signing up - the difference only appears once the link is
verified and `resolve_identity` finds a matching account.

```mermaid
sequenceDiagram
    participant U as User
    participant FE as Frontend
    participant API as API
    participant DB as Database

    U->>FE: Enters email, clicks Continue
    FE->>API: POST /auth/email/request {email}
    API-->>FE: "Check your email for the next step."

    U->>FE: Opens magic link from email
    FE->>API: GET /auth/email/verify/check?token=...
    API-->>FE: valid=true, email
    FE-->>U: Shows "Sign in" button

    U->>FE: Clicks "Sign in"
    FE->>API: POST /auth/email/verify {token}
    API->>DB: Validate token, mark used_at
    API->>DB: resolve_identity(email) -> account found
    API-->>FE: status=authenticated + access_token\n(+ refresh_token cookie)
    FE->>U: Redirect to /dashboard
```

#### 3. Sign in / sign up with Google

One endpoint (`POST /auth/google`) covers both a brand-new Google sign-in and one
for an account that already exists (either created via Google before, or via email
and now linking Google for the first time).

```mermaid
sequenceDiagram
    participant U as User
    participant FE as Frontend
    participant Google
    participant API as API
    participant DB as Database

    U->>FE: Clicks "Continue with Google"
    FE->>Google: Google Identity Services popup
    Google-->>FE: ID token (credential)
    FE->>API: POST /auth/google {credential}
    API->>Google: Verify ID token signature
    Google-->>API: sub, email (verified), name, avatar

    alt Google sub already linked to an account
        API->>DB: resolve_identity -> account found by provider id
        API-->>FE: status=authenticated + access_token
    else Account exists for this email (created via email link)
        API->>DB: resolve_identity -> account found by email
        API->>DB: Link Google provider to that account
        API-->>FE: status=authenticated + access_token
    else No account at all
        API-->>FE: status=onboarding_required\n+ onboarding_token, email, name, avatar
        FE->>U: Redirect to /onboarding (name prefilled)
        U->>FE: Confirms/edits name, picks username
        FE->>API: POST /auth/complete
        API->>DB: Create User + AuthenticationProvider\n(provider=google, provider_user_id=sub)
        API-->>FE: status=authenticated + access_token
    end
    FE->>U: Redirect to /dashboard
```

#### 4. Linking a second provider to an existing account

No explicit "link account" action exists - linking is a side effect of
`resolve_identity` recognizing the same email under a different provider.

```mermaid
sequenceDiagram
    participant U as User
    participant API as API
    participant DB as Database

    Note over U,DB: Account already exists, created via email magic link
    U->>API: POST /auth/google {credential} (same verified email)
    API->>DB: No AuthenticationProvider row for\n(provider=google, provider_user_id=sub)
    API->>DB: Look up account by email -> found
    API->>DB: Create AuthenticationProvider\n(user_id, provider=google, provider_user_id=sub)
    API-->>U: status=authenticated
    Note over U,DB: Account now has two rows in\nAuthenticationProviders: email + google
```

#### 5. Session refresh & logout

```mermaid
sequenceDiagram
    participant FE as Frontend
    participant API as API

    Note over FE,API: Access token expired, httpOnly\nrefresh_token cookie still valid
    FE->>API: POST /auth/refresh (cookie sent automatically)
    API->>API: Verify refresh token
    API-->>FE: New access_token

    Note over FE,API: User signs out
    FE->>API: POST /auth/logout (Bearer access_token)
    API->>API: Blacklist access + refresh token
    API-->>FE: refresh_token cookie cleared
```

#### 6. Changing an account's email

Shares the magic-link mechanics above, but requires an active session to start,
and a precheck on the confirmation page so a stale/already-used link never shows a
clickable button to begin with (see `DECISIONS.md`).

```mermaid
sequenceDiagram
    participant U as User
    participant FE as Frontend
    participant API as API
    participant DB as Database
    participant Mail as Resend

    U->>FE: Enters new email in Settings
    FE->>API: POST /user/email-change/request\n{new_email} (authenticated)
    API->>DB: Invalidate any previous live\nemail_change request for this user
    API->>DB: Create AuthenticationRequest\n(purpose=email_change, user_id, token_hash)
    API->>Mail: Send confirmation email to new_email
    API-->>FE: "Check your new email address\nto confirm the change."

    U->>Mail: Opens confirmation link
    Mail->>FE: GET /settings/confirm-email?token=...
    FE->>API: GET /user/email-change/verify/check?token=...
    alt Link already used/invalidated/expired
        API-->>FE: valid=false
        FE-->>U: Error shown immediately, no button
    else Link still live
        API-->>FE: valid=true, email (the new address)
        FE-->>U: Shows "Confirm email change to {email}"

        U->>FE: Clicks "Confirm email change"
        FE->>API: POST /user/email-change/verify {token}
        API->>DB: Validate token, update User.email,\nmark used_at (transactional)
        API->>Mail: Notify old address of the change
        API-->>FE: {email: new_email}
        FE-->>U: "Updated to {email}", auto-redirect\nto /settings after 3s
    end
```

Magic-link emails are sent via [Resend](https://resend.com). Set these in `src/.env`:

```bash
# Required to actually deliver magic-link emails - without it, the link is only
# logged (useful for local development).
RESEND_API_KEY="re_..._your_key"
EMAIL_FROM_ADDRESS="onboarding@resend.dev"

# Used to build the magic-link URL (`{FRONTEND_URL}/auth/verify?token=...`).
FRONTEND_URL="http://localhost:3000"

# Optional - tune magic-link/onboarding-session expiry and rate limits. See
# `MagicLinkSettings`/`CryptSettings` in `src/app/core/config.py` for all of them
# and their defaults.
MAGIC_LINK_TOKEN_EXPIRE_MINUTES=30
ONBOARDING_TOKEN_EXPIRE_MINUTES=30
EMAIL_CHANGE_TOKEN_EXPIRE_MINUTES=30
```

## Endpoints

### Parse a Suunto dive XML file

Upload a Suunto dive XML file and receive the parsed dive data as JSON:

```bash
curl -X POST http://localhost:8000/api/v1/dive/parse-xml \
  -F "file=@Dive_2021-04-06-1231.xml"
```
