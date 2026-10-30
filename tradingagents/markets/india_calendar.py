"""NSE and BSE equity-segment trading holidays and special sessions, as the exchanges publish them.

The exchanges publish the next year's list each December; add it here then. A
year missing here is not guessed: only its weekends count as closed, and the
session check says the calendar does not cover it. Holidays that fall on a
weekend are left out, since the exchange is closed then anyway.

2026 (checked 2026-10-29): NSE circular NSE/CMTR/71775 of 12 Dec 2025, with the
15 Jan holiday added by NSE/CMTR/72260 of 12 Jan 2026. BSE's 2026 list matches
it (BSE notices of the same dates, read through secondary copies). Special
sessions: the Union Budget session on Sunday 1 Feb 2026 (NSE/CMTR/72349) and
Muhurat trading on Sunday 8 Nov 2026 (NSE/CMTR/76941). Settlement holidays are a
separate NSE Clearing calendar and are not modelled here.
"""

from datetime import date, time

SOURCE = ("NSE circulars NSE/CMTR/71775 (12 Dec 2025) and NSE/CMTR/72260 (12 Jan 2026); "
          "special sessions NSE/CMTR/72349 and NSE/CMTR/76941")

NSE_HOLIDAYS: dict[date, str] = {
    date(2026, 1, 15): "Municipal Corporation Election, Maharashtra",
    date(2026, 1, 26): "Republic Day",
    date(2026, 3, 3): "Holi",
    date(2026, 3, 26): "Shri Ram Navami",
    date(2026, 3, 31): "Shri Mahavir Jayanti",
    date(2026, 4, 3): "Good Friday",
    date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day",
    date(2026, 5, 28): "Bakri Id",
    date(2026, 6, 26): "Muharram",
    date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 10): "Diwali-Balipratipada",
    date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
    date(2026, 12, 25): "Christmas",
}

BSE_HOLIDAYS: dict[date, str] = dict(NSE_HOLIDAYS)

HOLIDAY_YEARS: frozenset[int] = frozenset({2026})

# Days the exchange trades outside its weekday calendar: (name, pre-open start,
# regular open, regular close).
SPECIAL_SESSIONS: dict[date, tuple[str, time, time, time]] = {
    date(2026, 2, 1): ("Union Budget special session", time(9, 0), time(9, 15), time(15, 30)),
    date(2026, 11, 8): ("Muhurat trading (Diwali)", time(14, 5), time(14, 20), time(15, 30)),
}
