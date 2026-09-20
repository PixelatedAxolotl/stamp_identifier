"""index the foreign-key and lookup columns

Postgres indexes primary keys and unique constraints automatically but never
foreign keys, so every one of these columns was being sequentially scanned.
The cost is per *row looked up*, not per query, so it grew with the collection:
loading one country's stamps ran 477 queries, each a full scan of
stamp_images — about a second of frozen UI on every save.

stamps.country and stamps.added_to_db are here for the same reason but on the
read side: the Database panel filters by country and the Gallery orders by
added_to_db on every refresh.

The matching columns carry index=True in db/models.py so a database created by
create_all() (see db/session.py) comes out identical to a migrated one. The
names below are SQLAlchemy's default ix_<table>_<column>, so the two paths
agree and neither tries to create an index the other already made.

Revision ID: a3b4c5d6e7f8
Revises: c1d2e3f4a5b6
Create Date: 2026-08-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, Sequence[str], None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (index name, table, column)
_INDEXES = [
    ('ix_stamp_images_stamp_id',              'stamp_images',            'stamp_id'),
    ('ix_stamp_copies_stamp_id',              'stamp_copies',            'stamp_id'),
    ('ix_stamp_theme_association_theme_id',   'stamp_theme_association', 'theme_id'),
    ('ix_stamp_copy_origins_location_id',     'stamp_copy_origins',      'location_id'),
    ('ix_stamp_copy_origins_dealer_id',       'stamp_copy_origins',      'dealer_id'),
    ('ix_stamps_country',                     'stamps',                  'country'),
    ('ix_stamps_series_id',                   'stamps',                  'series_id'),
    ('ix_stamps_physical_location_id',        'stamps',                  'physical_location_id'),
    ('ix_stamps_variant_set_id',              'stamps',                  'variant_set_id'),
    ('ix_stamps_added_to_db',                 'stamps',                  'added_to_db'),
]


def upgrade() -> None:
    for name, table, column in _INDEXES:
        op.create_index(name, table, [column], if_not_exists=True)


def downgrade() -> None:
    for name, table, _column in reversed(_INDEXES):
        op.drop_index(name, table_name=table, if_exists=True)
