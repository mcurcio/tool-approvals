"""Alembic migrations for the Approvals schema. Each declares `phase`: "expand" (additive only) or "contract".

Expand-then-contract: the migration Job runs while the old release still serves, so a migration only adds until
the code that stops using a column or table has shipped; a migration marked "contract" then removes it.
`scripts/check_migrations.py` (CI) refuses a drop, a rename, a type change or a new rule in an expand migration.
"""
