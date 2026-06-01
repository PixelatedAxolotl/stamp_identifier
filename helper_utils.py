import pycountry

# Custom overrides / common abbreviations
COUNTRY_MAP = {
    "US": "United States of America",
    "BE": "Belgium",
    "UK": "United Kingdom",
    "FR": "France",
    "DE": "Germany",
    # Add any other special cases here
}

def get_country_name(code: str) -> str | None:
    """
    Translate a country abbreviation (ISO alpha-2 code) into its full name.
    First checks COUNTRY_MAP, then falls back to pycountry.
    Returns None if no match found.
    """
    if not code:
        return None

    code = code.upper()

    # Try map first
    if code in COUNTRY_MAP:
        return COUNTRY_MAP[code]

    # Then try pycountry
    try:
        country = pycountry.countries.get(alpha_2=code)
        if country:
            return country.name
    except Exception:
        pass

    # If all else fails, return None
    return None
