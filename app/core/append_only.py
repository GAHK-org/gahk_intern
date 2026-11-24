"""Tables whose rows are written once and never changed or deleted: the audit trail, the GAHK-konto
ledger.

Two layers. The models refuse a change before it reaches the database (`AppendOnly`), and a trigger
in the database refuses it whoever sends it (`trigger_sql`, run by each table's migration). TRUNCATE
is left alone: the test suite's flush uses it, and it takes the table owner, the same role that
could drop the trigger anyway.
"""

from typing import Any, NoReturn

from django.db import models


class AppendOnlyError(Exception):
    """A row of an append-only table was about to be changed or deleted."""


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs: Any) -> NoReturn:  # noqa: ANN401
        raise AppendOnlyError(f"Rows of {self.model.__name__} are never changed.")

    def delete(self) -> NoReturn:
        raise AppendOnlyError(f"Rows of {self.model.__name__} are never deleted.")


class AppendOnly(models.Model):
    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        if not self._state.adding:
            raise AppendOnlyError(f"Rows of {type(self).__name__} are never changed.")
        kwargs["force_insert"] = True
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:  # noqa: ANN401
        raise AppendOnlyError(f"Rows of {type(self).__name__} are never deleted.")


def trigger_sql(function: str, tables: tuple[str, ...], what: str) -> tuple[list[str], list[str]]:
    """(forwards, backwards) SQL for a migration: a trigger function refusing UPDATE and DELETE, and
    one trigger per table calling it. `what` names the tables in the error ("the audit trail")."""
    forwards = [
        f"""
        CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '{what} is append-only: % on % refused', TG_OP, TG_TABLE_NAME;
        END;
        $$
        """,
        *(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
            for table in tables
        ),
    ]
    backwards = [
        *(f"DROP TRIGGER {table}_append_only ON {table}" for table in tables),
        f"DROP FUNCTION {function}()",
    ]
    return forwards, backwards
