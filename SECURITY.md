# Security Policy

OpenDiving is self-hosted software: every instance is somebody's own server, holding their own dive
log. A defect in what we ship reaches all of them at once, so we would much rather hear about one
privately than read about it in a public issue.

This project is [AGPL-3.0](LICENSE) and run by a single maintainer in their spare time. There is no
bug bounty and no money behind any of this — what we can offer is a prompt reply, a fix in the next
release, and credit in the release notes if you want it.

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting:** the **Security** tab of this repository, then
**Report a vulnerability**. It opens a thread visible only to you and the maintainers, it keeps the
whole exchange attached to the repository, and it can become a published advisory with a CVE once
the fix is out.

If you have no GitHub account, or your report concerns a maintainer, **email
security@opendiving.app** instead. Reports there are read only by the project maintainers.

Whatever you can tell us helps, but the four things that speed a fix up most are:

1. The version you found it on — the image tag, or the commit if you are running from source.
2. What an attacker gets: someone else's dives, someone else's session, the whole database.
3. Enough to reproduce it — a request, a payload, a dive-computer file, a sequence of calls.
4. Whether anyone else knows, and any deadline you are working to.

Please test against an instance you run yourself. `docker compose up` gives you the whole stack in a
couple of minutes, and there is no shared deployment worth pointing a scanner at.

## What not to do

- **Don't open a public issue, pull request, or discussion** for a suspected vulnerability. Every
  self-hosted instance is exposed for as long as it takes to cut a release, and a public issue
  starts that clock before the fix exists. (You may see an open issue labelled `image-cve` naming
  CVEs in the published image. That is not an exception to this rule: those are advisories Debian
  and NVD published first, filed by our own scanner so the base-image rebuild gets done, and running
  `trivy image` against the same public tag tells you the same thing. This rule is about a defect in
  *our* code that nobody has disclosed yet — that still goes to the private channel above.)
- **Don't use the in-app contact form.** It has a *Security* category, and it is still the wrong
  route: `POST /api/v1/contact` delivers to whoever runs *that* instance, not to this project, and
  on most instances it delivers nowhere at all — `CONTACT_FORM_EMAIL` has no default, and unset the
  endpoint answers 503. Your report would reach a stranger's inbox or none.

## What to expect

One maintainer, best effort, no service-level agreement — but the ordinary shape of it is:

- An acknowledgement within a few days, from a human.
- A first assessment — in scope or not, and how severe it looks — once the report has been
  reproduced.
- Progress updates while a fix is being worked on, and a note when the release carrying it is out.
- Credit in the release notes under whatever name you choose, unless you would rather stay
  anonymous.

We will ask you to hold off on publishing until a release with the fix is available, and we will
tell you when that is rather than leaving you waiting indefinitely. If a report turns out not to be
a vulnerability we will say so plainly and explain why.

## Supported versions

| Version                 | Supported                  |
| ----------------------- | -------------------------- |
| The most recent release | Yes                        |
| Anything older          | No — upgrade to the newest |

This project is pre-1.0. Under `0.x`, a minor version is allowed to break things, and versions move
in lockstep with [opendiving-web](https://github.com/opendiving/opendiving-web) — one product
version tagged in both repos. There is no long-term support branch and no backports to older minors:
a security fix lands on `main` and ships in the next release, and self-hosters pick it up the way
they pick up everything else.

```bash
docker compose pull && docker compose up -d
```

Database migrations run themselves on startup, so upgrading is those two commands plus the release
notes — see [docs/self-hosting/upgrade.md](docs/self-hosting/upgrade.md). If you have pinned
`OPENDIVING_VERSION`, move the pin.

A published version is never repointed, so a fix in our own code always arrives as a *new* version
number rather than as a rebuilt tag you might already be running. The single exception is a
vulnerability in a base image, which is republished at the existing tag precisely so that everyone
following `latest` or `X.Y` gets it — see *Cutting a release* in [CONTRIBUTING.md](CONTRIBUTING.md).
In that case `docker compose pull` is the whole fix even with a version pinned.

## Scope

**In scope** — a defect in the code and configuration this project ships:

- The API and worker: authentication and magic-link handling, ownership and authorization checks,
  cache key scoping, upload handling and the dive-computer parsers, rate limiting, anything that
  serves one account's data to another.
- The admin panel as shipped, including its defaults.
- The install bundle — `deploy/docker-compose.yml`, `deploy/Caddyfile`, `deploy/example.env` — and
  anything unsafe about the configuration a fresh install ends up with by following
  [docs/self-hosting/install.md](docs/self-hosting/install.md).

**Out of scope** — how a particular instance was set up. Self-hosting puts real security decisions
on the operator, and [docs/self-hosting/](docs/self-hosting/) is where they are documented:

- `TRUSTED_PROXY_IPS` naming the wrong network, so the API believes an `X-Forwarded-For` it
  shouldn't and every per-IP rate limit reads the wrong address.
- The admin panel reachable from the internet without `CRUD_ADMIN_ALLOWED_IPS`/`..._NETWORKS`, or
  with credentials someone can guess.
- A weak `POSTGRES_PASSWORD` or `SECRET_KEY`, a Postgres port published to the world, an
  unmaintained host, an out-of-date reverse proxy.

The line is whether the report describes something *we* can fix by changing this repository. A
defect in the shipped code is report-worthy; a misconfiguration of your own instance is a support
question — start with [docs/self-hosting/troubleshooting.md](docs/self-hosting/troubleshooting.md)
and open a normal issue if that doesn't get you there.

Reports that are only a scanner's output, a missing hardening header with no demonstrated impact, or
a best-practice recommendation with no attack behind it are welcome as ordinary issues, but they are
not vulnerability reports and will be handled as the former.
