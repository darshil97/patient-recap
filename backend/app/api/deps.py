"""
Request dependencies.

AUTH IS TWO GATES, NOT ONE. They answer different questions and both must pass.

1. `require_auth` — *may this caller reach the API at all?*
   A shared secret in the `X-API-Key` header, enforced whenever `API_KEY` is set
   in settings and skipped entirely when it is not, so local development can stay
   open while a deployment only needs an environment variable. This is a network
   boundary: it authenticates the Next.js BFF as a trusted server, not a person.

2. `require_doctor` — *whose data is this request about?*
   The doctor-identity headers the BFF forwards from the signed-in Neon Auth
   session, resolved to a row in `doctors`. This is the tenancy boundary. Without
   it the backend would hold several doctors' clinical records and have no way to
   tell which one it was serving.

`require_doctor` depends on `require_auth`, so the key is still checked before any
identity is trusted. Header 1 without header 2 is a 401, not a fallback to some
default tenant: a code path that serves patient data without knowing whose it is
is the failure this whole change exists to remove.

THE TRUST CHAIN, STATED PLAINLY

    browser --(Neon Auth session cookie)--> Next.js BFF --(X-API-Key
              + X-Doctor-Auth-Id + X-Doctor-Email)--> FastAPI

FastAPI does **not** verify a Neon Auth token in this increment (a later one will,
against the JWKS endpoint). It trusts the BFF's assertion of who is signed in.
That trust is only sound because of three things, and it stops being sound if any
of them changes: the browser cannot reach FastAPI directly, the browser cannot
obtain `API_KEY`, and the BFF re-checks the session on every request it forwards.
Anyone holding `API_KEY` can therefore claim to be any doctor — so `API_KEY` is
now a tenancy-bearing secret, not just a rate-limiting one.

WHY THE HEADERS ARE DECLARED AS SECURITY SCHEMES

Both are `APIKeyHeader` rather than plain `Header(...)` parameters. As parameters
they would appear in `/openapi.json` under every endpoint's `parameters`, and
`npm run gen:api` would then emit them as required per-endpoint headers in
`frontend/lib/types/api.d.ts` — breaking every browser-side call site, which
cannot supply them because they are injected server-side in the BFF. As security
schemes they land in `components.securitySchemes`, which openapi-typescript
ignores, exactly as `X-API-Key` does today.

`GET /health` stays fully exempt from both gates so a monitor can check liveness
without holding the secret or being a doctor. `GET /health/api-keys` is *not*
exempt, because each call spends one request from every Gemini key's daily quota.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Iterator, Optional

import psycopg
from fastapi import Depends, Header, Security
from fastapi.security import APIKeyHeader

from app.config import Settings, get_settings
from app.core.errors import UnauthorizedError
from app.db.pool import connection
from app.db.repositories import doctors as doctors_repo

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"

# The stable one. Neon Auth's (Better Auth's) own subject id for the signed-in
# user, forwarded by the BFF from `session.user.id`.
DOCTOR_AUTH_ID_HEADER = "X-Doctor-Auth-Id"

# The human-readable one, from `session.user.email`. Needed for two things: it is
# how a doctor row is created the first time someone signs in, and it is how the
# row the seed migration created gets matched to a real account.
DOCTOR_EMAIL_HEADER = "X-Doctor-Email"

# Display only, used when creating a row. Never used to resolve one.
DOCTOR_NAME_HEADER = "X-Doctor-Name"

# auto_error=False so a missing header reaches require_auth, which decides
# whether auth is switched on at all and raises the project's own error type.
# Declared as a security scheme rather than a plain Header so it appears in
# OpenAPI as a requirement: /docs gets an Authorize button, and a generated
# client knows to send it.
api_key_scheme = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,
    scheme_name="ApiKeyAuth",
    description="Shared secret. Required when the server has API_KEY configured.",
)

doctor_auth_id_scheme = APIKeyHeader(
    name=DOCTOR_AUTH_ID_HEADER,
    auto_error=False,
    scheme_name="DoctorAuthId",
    description=(
        "The signed-in doctor's Neon Auth user id, forwarded by the Next.js BFF. "
        "Preferred over the email header because it never changes for an account."
    ),
)

doctor_email_scheme = APIKeyHeader(
    name=DOCTOR_EMAIL_HEADER,
    auto_error=False,
    scheme_name="DoctorEmail",
    description=(
        "The signed-in doctor's email, forwarded by the Next.js BFF. Used to "
        "create the doctor record on first sign-in, and to match the record "
        "seeded by sql/7_multi_tenancy.sql."
    ),
)


@dataclass(frozen=True)
class CurrentDoctor:
    """The resolved tenant for one request.

    Frozen because nothing downstream should be able to edit whose data it is
    looking at halfway through a request.
    """

    doctor_id: str
    email: str
    name: Optional[str] = None

    def __str__(self) -> str:  # pragma: no cover - logging aid
        return f"{self.email} ({self.doctor_id})"


def get_db() -> Iterator[psycopg.Connection]:
    """A pooled connection for the life of one request."""
    with connection() as conn:
        yield conn


def settings_dep() -> Settings:
    return get_settings()


async def require_auth(
    provided: Optional[str] = Security(api_key_scheme),
) -> None:
    """Enforce the shared secret when one is configured.

    Compared with hmac.compare_digest rather than `==` so the check cannot leak
    the key's length or content through response timing.
    """
    expected = get_settings().api_key
    if not expected:
        return  # open by design when unset

    if not provided or not hmac.compare_digest(provided, expected):
        raise UnauthorizedError(
            f"Missing or invalid API key. Send it in the {API_KEY_HEADER} header.")


async def require_doctor(
    conn: psycopg.Connection = Depends(get_db),
    _: None = Depends(require_auth),
    auth_user_id: Optional[str] = Security(doctor_auth_id_scheme),
    email: Optional[str] = Security(doctor_email_scheme),
    name: Optional[str] = Header(default=None, alias=DOCTOR_NAME_HEADER,
                                 include_in_schema=False),
) -> CurrentDoctor:
    """Resolve the signed-in doctor, creating their record on first sight.

    Resolution order is `auth_user_id` then `email`, and that order is the whole
    reason both headers exist:

      * `auth_user_id` never changes for an account, so it is the identity.
      * `email` is mutable — Better Auth ships a change-email flow — so keying on
        it alone would resolve a doctor who changed their address as a brand-new
        doctor with an empty patient list, while their real records became
        unreachable. It is still needed, because it is the only thing knowable
        before a doctor has ever signed in, which is what lets
        sql/7_multi_tenancy.sql create the first doctor row and hand it the 16
        existing patients.

    A row found by email has the auth id stamped onto it ("claimed") so every
    later request resolves by the stable key.

    Absent or unresolvable identity is a 401 rather than a silent default. There is
    no anonymous tenant to fall back to, and inventing one would mean a
    misconfigured BFF quietly pooled several doctors' patients into a single
    shared account instead of failing.
    """
    trimmed_auth_id = (auth_user_id or "").strip() or None
    trimmed_email = (email or "").strip().lower() or None

    if not trimmed_auth_id and not trimmed_email:
        raise UnauthorizedError(
            f"No doctor identity on this request. The API gateway must forward "
            f"the signed-in doctor in {DOCTOR_AUTH_ID_HEADER} and "
            f"{DOCTOR_EMAIL_HEADER}.",
            details={"required_headers": [DOCTOR_AUTH_ID_HEADER,
                                          DOCTOR_EMAIL_HEADER]},
        )

    if trimmed_email and "@" not in trimmed_email:
        # Rejected rather than ignored: falling back to the auth id here would
        # create a doctor row with a junk email, and that row is what every
        # patient in the practice gets foreign-keyed to.
        raise UnauthorizedError(
            f"{DOCTOR_EMAIL_HEADER} is not an email address.",
            details={"header": DOCTOR_EMAIL_HEADER},
        )

    doctor = doctors_repo.resolve(
        conn,
        auth_user_id=trimmed_auth_id,
        email=trimmed_email,
        name=(name or "").strip() or None,
    )

    if doctor is None:
        # An auth id we have never seen, with no email to attach it to. Creating a
        # nameless tenant would split one doctor's records across two rows the
        # first time the email header went missing.
        raise UnauthorizedError(
            f"Could not identify the signed-in doctor. {DOCTOR_AUTH_ID_HEADER} is "
            f"unrecognised and no {DOCTOR_EMAIL_HEADER} was supplied to register "
            f"it against.",
            details={"required_headers": [DOCTOR_EMAIL_HEADER]},
        )

    return CurrentDoctor(doctor_id=doctor.doctor_id, email=doctor.email,
                         name=doctor.name)


DbConnection = Depends(get_db)
AppSettings = Depends(settings_dep)
RequireAuth = Depends(require_auth)
CurrentDoctorDep = Depends(require_doctor)
