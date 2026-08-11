"""add owned column

Revision ID: f1e2d3c4b5a6
Revises: e3f9a12b4c87
Create Date: 2026-06-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1e2d3c4b5a6'
down_revision: Union[str, Sequence[str], None] = 'e3f9a12b4c87'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Default True so all existing stamps are treated as owned.
    op.add_column(
        'stamps',
        sa.Column('owned', sa.Boolean(), nullable=False, server_default='true'),
    )


def downgrade() -> None:
    op.drop_column('stamps', 'owned')
