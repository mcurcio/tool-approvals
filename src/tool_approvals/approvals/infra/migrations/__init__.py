"""Alembic migrations for the Approvals schema. Each declares `phase`: "expand" (additive only) or "contract".

Expand-then-contract: the migration Job runs while the old pod still serves, so a migration may only add
until the code that stops using a column or table has shipped; then a migration marked "contract" removes
it. `scripts/check_migrations.py` (CI) refuses a drop or a type change in any migration not marked contract.
"""
