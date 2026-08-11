"""add series table, migrate series_url/series_complete off stamps

Revision ID: f6a1b2c3d4e5
Revises: e5f6a1b2c3d4
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f6a1b2c3d4e5'
down_revision: Union[str, Sequence[str], None] = 'e5f6a1b2c3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create series table
    op.create_table(
        'series',
        sa.Column('id',              sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name',            sa.String(),  nullable=False),
        sa.Column('series_url',      sa.String(),  nullable=True,  unique=True),
        sa.Column('series_complete', sa.String(),  nullable=True),
        sa.Column('comments',        sa.Text(),    nullable=True),
    )

    # 2. Add series_id to stamps (no FK yet — add after data is populated)
    op.add_column('stamps', sa.Column('series_id', sa.Integer(), nullable=True))

    # 3. Populate series from distinct non-null series_url values on stamps
    op.execute("""
        INSERT INTO series (name, series_url, series_complete)
        SELECT DISTINCT ON (series_url)
            COALESCE(series, ''),
            series_url,
            series_complete
        FROM stamps
        WHERE series_url IS NOT NULL AND series_url <> ''
        ORDER BY series_url, id
    """)

    # 4. Back-fill series_id on stamps
    op.execute("""
        UPDATE stamps
        SET series_id = (
            SELECT se.id FROM series se
            WHERE se.series_url = stamps.series_url
            LIMIT 1
        )
        WHERE stamps.series_url IS NOT NULL AND stamps.series_url <> ''
    """)

    # 5. Add FK constraint now that data is consistent
    op.create_foreign_key(
        'fk_stamps_series_id', 'stamps', 'series',
        ['series_id'], ['id'], ondelete='SET NULL',
    )

    # 6. Drop the old columns from stamps
    op.drop_column('stamps', 'series_url')
    op.drop_column('stamps', 'series_complete')


def downgrade() -> None:
    op.add_column('stamps', sa.Column('series_complete', sa.String(), nullable=True))
    op.add_column('stamps', sa.Column('series_url',      sa.String(), nullable=True))

    op.execute("""
        UPDATE stamps
        SET series_url      = se.series_url,
            series_complete = se.series_complete
        FROM series se
        WHERE stamps.series_id = se.id
    """)

    op.drop_constraint('fk_stamps_series_id', 'stamps', type_='foreignkey')
    op.drop_column('stamps', 'series_id')
    op.drop_table('series')
