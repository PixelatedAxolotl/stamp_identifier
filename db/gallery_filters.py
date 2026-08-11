# db/gallery_filters.py
#
# Field registry + rule->SQLAlchemy-clause builder for the gallery filter
# builder. This is the single source of truth the UI (which fields exist, what
# operators/value editor each one gets) and the query layer (how each rule
# becomes a WHERE clause) both read from, so the two can't drift apart.
#
# A "rule" is a plain dict produced by the UI:
#     {"field": "perforation", "op": "contains", "value": "12"}
#     {"field": "country",     "op": "is",       "value": "USA"}
#     {"field": "owned",       "op": "yes"}                       # value unused
#
# v1 constraint: every rule carries at most one value (no multi-select, no
# range/between operators). Both are intentional and can be relaxed later.

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from db.models import (
    Stamp, Theme, Series, PhysicalLocation, VariantSet, StampCopy,
)


class FT(Enum):
    """Field kinds — drive both the value editor and the clause builder."""
    TEXT     = "text"      # free-text column: contains / is / empty
    ENUM     = "enum"      # Stamp column, chosen from its distinct values
    BOOL     = "bool"      # boolean column: yes / no
    NUMBER   = "number"    # numeric column: = / > / < / empty
    DATE     = "date"      # datetime column: after / before / empty
    RELATION = "relation"  # a related row, matched by name / condition


@dataclass(frozen=True)
class FieldSpec:
    key: str                       # rule["field"] and, for non-relations, the Stamp attr
    label: str                     # shown in the field dropdown
    type: FT
    relation: str | None = None    # RELATION only: which relation (see _RELATION_CLAUSE)


# Order here is the order shown in the field dropdown.
FIELDS: list[FieldSpec] = [
    FieldSpec("title",        "Title",        FT.TEXT),
    FieldSpec("scott_number", "Scott #",      FT.TEXT),
    FieldSpec("country",      "Country",      FT.ENUM),
    FieldSpec("format",       "Format",       FT.ENUM),
    FieldSpec("paper",        "Paper",        FT.ENUM),
    FieldSpec("gum",          "Gum",          FT.ENUM),
    FieldSpec("printing",     "Printing",     FT.ENUM),
    FieldSpec("watermark",    "Watermark",    FT.ENUM),
    FieldSpec("emission",     "Emission",     FT.ENUM),
    FieldSpec("face_value",   "Face value",   FT.TEXT),
    FieldSpec("colors",       "Colors",       FT.TEXT),
    FieldSpec("designers",    "Designers",    FT.TEXT),
    FieldSpec("size",         "Size",         FT.TEXT),
    FieldSpec("perforation",  "Perforation",  FT.TEXT),
    FieldSpec("description",  "Description",  FT.TEXT),
    FieldSpec("print_run",    "Print run",    FT.NUMBER),
    FieldSpec("issued_date",  "Issued date",  FT.DATE),
    FieldSpec("expired_date", "Expired date", FT.DATE),
    FieldSpec("owned",        "Owned",        FT.BOOL),
    FieldSpec("variants",     "Has variants", FT.BOOL),
    FieldSpec("series",       "Series",       FT.RELATION, relation="series"),
    FieldSpec("themes",       "Tag",          FT.RELATION, relation="themes"),
    FieldSpec("location",     "Location",     FT.RELATION, relation="location"),
    FieldSpec("variant_set",  "Variant set",  FT.RELATION, relation="variant_set"),
    FieldSpec("condition",    "Condition",    FT.RELATION, relation="condition"),
]

FIELDS_BY_KEY: dict[str, FieldSpec] = {f.key: f for f in FIELDS}

# Operators offered per field type: (op_id, label). op_id is stored in the rule
# and dispatched in build_clause; label is shown in the operator dropdown.
OPS: dict[FT, list[tuple[str, str]]] = {
    FT.TEXT:     [("contains", "contains"), ("is", "is"),
                  ("empty", "is empty"), ("nempty", "is not empty")],
    FT.ENUM:     [("is", "is"), ("isnot", "is not")],
    FT.BOOL:     [("yes", "yes"), ("no", "no")],
    FT.NUMBER:   [("eq", "="), ("gt", ">"), ("lt", "<"), ("empty", "is empty")],
    FT.DATE:     [("after", "after"), ("before", "before"), ("empty", "is empty")],
    FT.RELATION: [("has", "has"), ("hasnot", "does not have")],
}

# Operators that need no value widget — the operator itself is the whole rule.
VALUELESS_OPS = {"empty", "nempty", "yes", "no"}


def _relation_clause(relation: str, op: str, value):
    """Build a clause for a RELATION field. op is 'has' / 'hasnot'."""
    exists = {
        "series":      Stamp.series_obj.has(Series.name == value),
        "themes":      Stamp.themes.any(Theme.name == value),
        "location":    Stamp.physical_location.has(PhysicalLocation.name == value),
        "variant_set": Stamp.variant_set.has(VariantSet.name == value),
        "condition":   Stamp.copies.any(StampCopy.condition == value),
    }.get(relation)
    if exists is None:
        return None
    return ~exists if op == "hasnot" else exists


def _coerce_number(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def _coerce_date(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt)
        except ValueError:
            continue
    return None


def build_clause(rule: dict):
    """Translate one rule dict into a SQLAlchemy clause, or None to skip it.

    None is returned for unknown fields and for value-carrying rules whose value
    is missing/uncoercible, so a half-built row in the UI is simply ignored
    rather than raising.
    """
    spec = FIELDS_BY_KEY.get(rule.get("field"))
    if spec is None:
        return None
    op = rule.get("op")
    value = rule.get("value")

    # Value-carrying ops need a non-empty value; valueless ops never do.
    if op not in VALUELESS_OPS and (value is None or value == ""):
        return None

    if spec.type == FT.TEXT:
        col = getattr(Stamp, spec.key)
        if op == "contains":
            return col.ilike(f"%{value}%")
        if op == "is":
            return col == value
        if op == "empty":
            return (col.is_(None)) | (col == "")
        if op == "nempty":
            return (col.isnot(None)) & (col != "")

    elif spec.type == FT.ENUM:
        col = getattr(Stamp, spec.key)
        return col == value if op == "is" else col != value

    elif spec.type == FT.BOOL:
        return getattr(Stamp, spec.key).is_(op == "yes")

    elif spec.type == FT.NUMBER:
        col = getattr(Stamp, spec.key)
        if op == "empty":
            return col.is_(None)
        num = _coerce_number(value)
        if num is None:
            return None
        return {"eq": col == num, "gt": col > num, "lt": col < num}.get(op)

    elif spec.type == FT.DATE:
        col = getattr(Stamp, spec.key)
        if op == "empty":
            return col.is_(None)
        dt = _coerce_date(value)
        if dt is None:
            return None
        return {"after": col >= dt, "before": col <= dt}.get(op)

    elif spec.type == FT.RELATION:
        return _relation_clause(spec.relation, op, value)

    return None
