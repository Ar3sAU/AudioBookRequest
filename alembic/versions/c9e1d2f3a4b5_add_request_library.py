"""add library choice to requests

Revision ID: c9e1d2f3a4b5
Revises: b8d4f0e2a3c5
Create Date: 2026-11-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9e1d2f3a4b5"
down_revision: str | None = "b8d4f0e2a3c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("audiobookrequest", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("library_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("move_abs_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("audiobookrequest", schema=None) as batch_op:
        batch_op.drop_column("move_abs_id")
        batch_op.drop_column("library_id")
