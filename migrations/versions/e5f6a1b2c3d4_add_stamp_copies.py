"""add stamp copies

Revision ID: e5f6a1b2c3d4
Revises: d4e5f6a1b2c3
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5f6a1b2c3d4'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a1b2c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'stamp_copies',
        sa.Column('id',        sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('stamp_id',  sa.Integer(),
                  sa.ForeignKey('stamps.id', ondelete='CASCADE'), nullable=False),
        sa.Column('condition', sa.String(),  nullable=False),
        sa.Column('quantity',  sa.Integer(), nullable=False, server_default='1'),
        sa.Column('notes',     sa.String(),  nullable=True),
    )


def downgrade() -> None:
    op.drop_table('stamp_copies')
