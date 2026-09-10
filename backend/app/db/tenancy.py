"""
The tenancy boundary, expressed as a type.

Every repository read and write against `patients`, `visits`, `visit_insights`,
`follow_ups` and `insight_jobs` takes a `DoctorScope` as its **second positional
argument**, directly after the connection. That placement is deliberate: a call
site that forgets it raises `TypeError` before any SQL runs, rather than quietly
returning another doctor's patients. A missing tenancy filter is a data leak
between doctors, not a bug in a listing, so the goal is to make it impossible to
omit by accident.

For the same reason "no filter" is a value, not an absence:

    patients_repo.list_all(conn, doctor_id)          # one doctor
    patients_repo.list_all(conn, ALL_DOCTORS)        # deliberately every doctor

`ALL_DOCTORS` is a distinct sentinel object rather than `None` so that an unset
variable or a mistyped keyword cannot silently disable scoping — `None` raises,
`ALL_DOCTORS` is something a developer has to type. It exists for exactly two
callers, both of them outside the HTTP request path:

  * `cli/load_insights.py`, which is run locally by the operator against their
    own database and is privileged by design (see §8.20)
  * `jobs_repo.fail_orphans`, which runs at startup with no request context and
    must reap every abandoned job regardless of whose it was

Nothing under `app/api/` may use it.

Tenancy is derived, never denormalised. `patients.doctor_id` is the only place a
doctor id is stored; visits, insights, follow-ups and jobs reach it through their
existing foreign keys. See §6 for why that beats a `doctor_id` column on every
table.
"""

from __future__ import annotations

from typing import Any, Final, List, Tuple, Union


class _AllDoctors:
    """Sentinel type for a deliberately unscoped query. Not instantiable twice."""

    _instance: "_AllDoctors | None" = None

    def __new__(cls) -> "_AllDoctors":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ALL_DOCTORS"

    def __bool__(self) -> bool:
        """Always truthy, so `if not scope:` cannot mistake it for a missing id."""
        return True


ALL_DOCTORS: Final[_AllDoctors] = _AllDoctors()

#: Either one doctor's id, or the explicit cross-doctor sentinel.
DoctorScope = Union[str, _AllDoctors]

# The canonical path from any tenant-owned row back to its owning doctor.
# visits, visit_insights, follow_ups and insight_jobs all resolve through this.
PATIENT_DOCTOR_COLUMN = "p.doctor_id"


def is_scoped(scope: DoctorScope) -> bool:
    """True when the scope names one doctor."""
    if scope is ALL_DOCTORS:
        return False
    if isinstance(scope, str) and scope:
        return True
    raise ValueError(
        f"Invalid doctor scope {scope!r}. Pass a doctor_id, or ALL_DOCTORS to "
        f"query across every doctor on purpose."
    )


def doctor_id_of(scope: DoctorScope) -> str:
    """The doctor id, for writes that must record ownership.

    A write cannot be cross-doctor: there is no row to insert if we do not know
    whose it is, so ALL_DOCTORS is refused here rather than defaulted.
    """
    if not is_scoped(scope):
        raise ValueError(
            "A write needs a specific doctor_id; ALL_DOCTORS is read-only."
        )
    return str(scope)


def clause(
    scope: DoctorScope,
    column: str = PATIENT_DOCTOR_COLUMN,
) -> Tuple[List[str], List[Any]]:
    """WHERE fragments and parameters that confine a query to one doctor.

    Returns empty lists for ALL_DOCTORS, which is the only way to get an
    unfiltered query out of this module.
    """
    if not is_scoped(scope):
        return [], []
    return [f"{column} = %s"], [str(scope)]


def visit_owner_exists(alias: str = "v") -> str:
    """An EXISTS predicate proving a visit belongs to the scoped doctor.

    Used by tables that reach `patients` through `visits` rather than joining it
    directly, so the SELECT column list stays untouched.
    """
    return (
        f"EXISTS (SELECT 1 FROM visits _v"
        f"         JOIN patients _p ON _p.patient_id = _v.patient_id"
        f"        WHERE _v.visit_id = {alias} AND _p.doctor_id = %s)"
    )
