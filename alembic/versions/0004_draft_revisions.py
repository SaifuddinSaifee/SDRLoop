"""draft_revisions: rewrites a human asked for in words

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "draft_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.Integer(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("before", sa.JSON(), nullable=False),
        sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["drafts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id", "n"),
    )


def downgrade() -> None:
    op.drop_table("draft_revisions")
