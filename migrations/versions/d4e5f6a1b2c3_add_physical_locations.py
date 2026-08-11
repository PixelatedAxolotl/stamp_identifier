"""add physical locations

Revision ID: d4e5f6a1b2c3
Revises: f1e2d3c4b5a6
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4e5f6a1b2c3'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'physical_locations',
        sa.Column('id',   sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(),  nullable=False, unique=True),
    )
    op.add_column(
        'stamps',
        sa.Column(
            'physical_location_id',
            sa.Integer(),
            sa.ForeignKey('physical_locations.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('stamps', 'physical_location_id')
    op.drop_table('physical_locations')
