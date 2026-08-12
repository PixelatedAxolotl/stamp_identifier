"""add copy origins (acquisition provenance)

Revision ID: b7c8d9e0f1a2
Revises: a2b3c4d5e6f7
Create Date: 2026-08-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, Sequence[str], None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'origin_locations',
        sa.Column('id',   sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(),  nullable=False, unique=True),
    )
    op.create_table(
        'dealers',
        sa.Column('id',   sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(),  nullable=False, unique=True),
    )
    op.create_table(
        'stamp_copy_origins',
        sa.Column('id',            sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('copy_id',       sa.Integer(),
                  sa.ForeignKey('stamp_copies.id', ondelete='CASCADE'),
                  nullable=False, unique=True),
        sa.Column('location_id',   sa.Integer(),
                  sa.ForeignKey('origin_locations.id', ondelete='SET NULL'), nullable=True),
        sa.Column('dealer_id',     sa.Integer(),
                  sa.ForeignKey('dealers.id', ondelete='SET NULL'), nullable=True),
        sa.Column('method',        sa.String(),        nullable=True),
        sa.Column('price',         sa.Numeric(10, 2),  nullable=True),
        sa.Column('acquired_date', sa.DateTime(),      nullable=True),
        sa.Column('notes',         sa.String(),        nullable=True),
    )


def downgrade() -> None:
    op.drop_table('stamp_copy_origins')
    op.drop_table('dealers')
    op.drop_table('origin_locations')
