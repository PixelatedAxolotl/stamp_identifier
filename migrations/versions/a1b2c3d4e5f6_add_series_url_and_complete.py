"""add series_url and series_complete columns

Revision ID: a1b2c3d4e5f6
Revises: f1e2d3c4b5a6
Create Date: 2026-06-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'f1e2d3c4b5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('stamps', sa.Column('series_url', sa.String(), nullable=True))
    op.add_column('stamps', sa.Column('series_complete', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('stamps', 'series_complete')
    op.drop_column('stamps', 'series_url')
