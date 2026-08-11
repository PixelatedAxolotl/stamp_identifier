"""add variant sets

Revision ID: e3f9a12b4c87
Revises: 796f688b4fcf
Create Date: 2026-06-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e3f9a12b4c87'
down_revision: Union[str, Sequence[str], None] = '796f688b4fcf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'variant_sets',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.add_column(
        'stamps',
        sa.Column('variant_set_id', sa.Integer(),
                  sa.ForeignKey('variant_sets.id', ondelete='SET NULL'),
                  nullable=True),
    )


def downgrade() -> None:
    op.drop_column('stamps', 'variant_set_id')
    op.drop_table('variant_sets')
