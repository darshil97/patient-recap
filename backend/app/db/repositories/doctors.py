"""
Doctor records: the anchor for tenancy, and nothing more.

This is **not** user management. Neon Auth owns sign-in, sessions, passwords and
email verification. This table exists so `patients.doctor_id` has something to
foreign-key against, and so an identity survives a change of email address.

Resolution order matters and is the reason there are two columns:

    auth_user_id   Neon Auth's own subject id. Preferred, because it never
                   changes for a given account.
    email          Fallback, and the key the seed migration used because it is
                   the only thing knowable before a doctor has ever signed in.

A row found by email gets its `auth_user_id` stamped on first sign-in ("claimed")
so every subsequent request resolves by the stable key. Without that, a doctor
who used Better Auth's change-email flow would be resolved as a brand-new doctor
with an empty patient list while their real records became unreachable.

The fallback is one-way. An *unclaimed* row is claimed by the auth id presented;
a row already claimed by a **different** auth id is refused, not served. Without
that second rule the fallback quietly inverted the preference order -- any caller
naming the right email resolved the tenant regardless of which account they were
signed in as, making email the real identity and `auth_user_id` decorative.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import psycopg

from app.db.models import DoctorRow

logger = logging.getLogger(__name__)

_COLUMNS = "doctor_id::text, email, auth_user_id, name, created_at"


def _row(record: Optional[Dict[str, Any]]) -> Optional[DoctorRow]:
    return DoctorRow(**record) if record else None


def get(conn: psycopg.Connection, doctor_id: str) -> Optional[DoctorRow]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM doctors WHERE doctor_id = %s",
                    (doctor_id,))
        return _row(cur.fetchone())


def get_by_auth_user_id(conn: psycopg.Connection,
                        auth_user_id: str) -> Optional[DoctorRow]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM doctors WHERE auth_user_id = %s",
                    (auth_user_id,))
        return _row(cur.fetchone())


def get_by_email(conn: psycopg.Connection, email: str) -> Optional[DoctorRow]:
    """Case-insensitive, matching the ux_doctors_email_lower unique index.

    'Doctor@Example.com' and 'doctor@example.com' are one mailbox; treating them
    as two would create two tenants for one person.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM doctors "
            f"WHERE lower(btrim(email)) = lower(btrim(%s))",
            (email,),
        )
        return _row(cur.fetchone())


def claim(conn: psycopg.Connection, doctor_id: str,
          auth_user_id: str) -> Optional[DoctorRow]:
    """Bind a Neon Auth subject id to an existing doctor row, once.

    `auth_user_id IS NULL` in the WHERE makes this a no-op if the row is already
    claimed, so two concurrent first requests cannot fight over it and a row can
    never be silently re-pointed at a different account.
    """
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE doctors SET auth_user_id = %s
               WHERE doctor_id = %s AND auth_user_id IS NULL""",
            (auth_user_id, doctor_id),
        )
    return get(conn, doctor_id)


def create(
    conn: psycopg.Connection,
    email: str,
    auth_user_id: Optional[str] = None,
    name: Optional[str] = None,
) -> DoctorRow:
    """Register a doctor on first sight.

    ON CONFLICT DO NOTHING then re-read, rather than ON CONFLICT DO UPDATE: two
    requests from the same new doctor can arrive together (the UI fires several
    fetches on first paint), and both must end up with the same row instead of
    one of them failing on the unique index. The conflict target is the
    functional index on lower(btrim(email)), which is why it is written as an
    expression rather than a column name.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO doctors (email, auth_user_id, name)
                VALUES (lower(btrim(%s)), %s, %s)
                ON CONFLICT (lower(btrim(email))) DO NOTHING
                RETURNING {_COLUMNS}""",
            (email, auth_user_id, name),
        )
        created = _row(cur.fetchone())
    if created is not None:
        return created
    # Lost the race: the other request inserted it. Its row is just as good.
    existing = get_by_email(conn, email)
    if existing is None:  # pragma: no cover - only if the row vanished mid-flight
        raise RuntimeError(f"Could not create or find a doctor for {email!r}.")
    return existing


def resolve(
    conn: psycopg.Connection,
    auth_user_id: Optional[str],
    email: Optional[str],
    name: Optional[str] = None,
) -> Optional[DoctorRow]:
    """Turn a Neon Auth identity into a doctor row, creating one if needed.

    Returns None when neither identifier was supplied, which the caller turns
    into a 401. This backend is about to hold several doctors' clinical data, so
    "I could not tell who is asking" must fail the request rather than fall
    through to some default tenant.
    """
    if auth_user_id:
        found = get_by_auth_user_id(conn, auth_user_id)
        if found is not None:
            return found

    if not email:
        # An auth id we have never seen and no email to attach it to. Refusing is
        # the only safe answer: creating a nameless tenant would silently split
        # one doctor's records across two rows.
        return None

    by_email = get_by_email(conn, email)
    if by_email is not None:
        # Pre-seeded by the migration, or created before we knew the subject id.
        if auth_user_id and not by_email.auth_user_id:
            return claim(conn, by_email.doctor_id, auth_user_id) or by_email

        if auth_user_id and by_email.auth_user_id != auth_user_id:
            # The row is already bound to a *different* Neon Auth account.
            #
            # Returning it anyway made email, not auth_user_id, the effective
            # identity -- which is the opposite of the order this function exists
            # to enforce. claim() already refuses to re-point a claimed row
            # (`AND auth_user_id IS NULL`), but nothing stopped that row being
            # *served* to a mismatched id, so the protection only covered the
            # write.
            #
            # There is no safe reading of this. Either the address moved between
            # two accounts, or someone is presenting an email that is not theirs.
            # Both need a human, so refuse and let the caller raise 401.
            logger.warning(
                "Doctor %s is bound to a different auth_user_id than the one "
                "presented; refusing to resolve by email alone.",
                by_email.doctor_id)
            return None

        return by_email

    return create(conn, email=email, auth_user_id=auth_user_id, name=name)
