"""National public holidays, offline (the `holidays` package: nothing is sent anywhere). A person's region (tables.REGIONS, chosen at
signup or in settings) gives their holidays; a company's are its location's country. They're shown on the tasks calendar, and no task
can be given on a company holiday or be due on a company holiday or on any assignee's national holiday (routers/tasks.py)."""
from datetime import date
from functools import lru_cache
import holidays
from system import templates
from tables import REGION_ZONES, REGIONS
@lru_cache(maxsize=256)
def _year(country: str, year: int) -> dict[date, str]:
    try: h = holidays.country_holidays(country, years=year, language="en_US")  # English names where the country has them
    except (NotImplementedError, KeyError, ValueError):
        try: h = holidays.country_holidays(country, years=year)
        except (NotImplementedError, KeyError, ValueError): return {}  # a country the package doesn't know: no holidays
    return dict(h)
def holiday_on(country: str | None, day: date) -> str | None:  # the holiday's name, or None
    return _year(country.upper(), day.year).get(day) if country else None
def holidays_between(country: str | None, start: date, end: date) -> dict[date, str]:  # start..end inclusive
    if not country: return {}
    out = {}
    for y in range(start.year, end.year + 1): out.update({d: n for d, n in _year(country.upper(), y).items() if start <= d <= end})
    return out
def region_name(code: str | None) -> str: return REGIONS.get(code or "", ("not set", ""))[0]
def currency_of(code: str | None) -> str | None: return REGIONS.get(code or "", (None, None))[1]
templates.env.globals.update(region_name=region_name, currency_of=currency_of, regions_list=REGIONS, region_zones=REGION_ZONES)  # {{ region_name('IN') }}, the zone picker
