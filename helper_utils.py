import json
import os
import pycountry
from config import COUNTRY_OVERRIDES_FILE, TAG_ALIASES_FILE, THEME_IMPLICATIONS_FILE

# Live map — populated from country_overrides.json, updated in-place so all
# callers see changes without re-importing
COUNTRY_MAP: dict[str, str] = {}


def reload_country_overrides() -> None:
    """Re-read country_overrides.json and update COUNTRY_MAP in-place.
    Safe to call at any time; callers holding a reference to COUNTRY_MAP
    automatically see the new data because the dict object is mutated."""
    COUNTRY_MAP.clear()
    try:
        path = COUNTRY_OVERRIDES_FILE
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                overrides = json.load(f)
            if isinstance(overrides, dict):
                for k, v in overrides.items():
                    if k and v:
                        COUNTRY_MAP[str(k).upper()] = str(v)
    except Exception:
        pass


# Initial load
reload_country_overrides()


def get_country_name(code: str) -> str | None:
    """
    Translate a country code or abbreviation into its full name.
    Checks COUNTRY_MAP first (any length, case-insensitive), then pycountry.
    Returns None if no match found.
    """
    if not code:
        return None

    code = code.strip()
    if not code:
        return None

    # Check COUNTRY_MAP for any-length code (covers both ISO overrides and
    # custom codes like "CSA", "DDR", "USSR" added by the user)
    key = code.upper()
    if key in COUNTRY_MAP:
        return COUNTRY_MAP[key]

    # For 2-letter codes not in the map, try pycountry alpha-2
    if len(code) == 2 and code.isalpha():
        try:
            country = pycountry.countries.get(alpha_2=key)
            if country:
                return country.name
        except Exception:
            pass

    # Try matching against the values in COUNTRY_MAP (full-name lookup)
    for v in COUNTRY_MAP.values():
        if str(v).strip().lower() == code.lower():
            return v

    # Fall back to pycountry fuzzy name search
    try:
        country = pycountry.countries.search_fuzzy(code)
        if country:
            return country[0].name
    except Exception:
        pass

    return None


def load_tag_aliases() -> dict:
    """Load tag alias mappings from JSON. Keys are Colnect names, values are local names."""
    try:
        if TAG_ALIASES_FILE and os.path.exists(TAG_ALIASES_FILE):
            with open(TAG_ALIASES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {str(k).strip(): str(v).strip() for k, v in data.items() if k and v}
    except Exception:
        pass
    return {}


def save_tag_aliases(aliases: dict) -> None:
    """Persist tag alias mappings to JSON."""
    try:
        with open(TAG_ALIASES_FILE, "w", encoding="utf-8") as f:
            json.dump(aliases, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def load_theme_implications() -> dict:
    """Load theme implication mappings. Keys are trigger themes, values are lists of implied themes."""
    try:
        if THEME_IMPLICATIONS_FILE and os.path.exists(THEME_IMPLICATIONS_FILE):
            with open(THEME_IMPLICATIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    result = {}
                    for k, v in data.items():
                        k = str(k).strip()
                        if k and isinstance(v, list):
                            result[k] = [str(x).strip() for x in v if x and str(x).strip()]
                    return result
    except Exception:
        pass
    return {}


def save_theme_implications(implications: dict) -> None:
    """Persist theme implication mappings to JSON."""
    try:
        with open(THEME_IMPLICATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(implications, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
