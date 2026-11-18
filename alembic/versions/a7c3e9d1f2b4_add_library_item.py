"""add library item table for Audiobookshelf library sync

Revision ID: a7c3e9d1f2b4
Revises: f1a2b3c4d5e6
Create Date: 2026-11-18 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e9d1f2b4"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "libraryitem",
        sa.Column("abs_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("library_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("asin", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("subtitle", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("norm_title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("authors", sa.JSON(), nullable=True),
        sa.Column("narrators", sa.JSON(), nullable=True),
        sa.Column("series", sa.JSON(), nullable=True),
        sa.Column("duration_min", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("abs_id"),
    )
    with op.batch_alter_table("libraryitem", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_libraryitem_library_id"), ["library_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_libraryitem_asin"), ["asin"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_libraryitem_norm_title"), ["norm_title"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("libraryitem", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_libraryitem_norm_title"))
        batch_op.drop_index(batch_op.f("ix_libraryitem_asin"))
        batch_op.drop_index(batch_op.f("ix_libraryitem_library_id"))
    op.drop_table("libraryitem")
