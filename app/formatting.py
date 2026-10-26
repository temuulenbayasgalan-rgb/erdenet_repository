"""Jinja filters for showing money, dates and times the Mongolian way."""
from datetime import timezone

from app.validation import OFFICE_TZ

WEEKDAYS = ("Даваа", "Мягмар", "Лхагва", "Пүрэв", "Баасан", "Бямба", "Ням")


def mnt(amount):
    """80000 -> '80 000 ₮'"""
    if amount is None:
        return ""
    return f"{amount:,}".replace(",", " ") + " ₮"


def mn_date(value):
    """2026-10-28 -> '2026-10-28 (Лхагва)'"""
    if value is None:
        return ""
    return f"{value:%Y-%m-%d} ({WEEKDAYS[value.weekday()]})"


def hm(value):
    return value.strftime("%H:%M") if value else ""


def mn_datetime(value):
    """A stored UTC timestamp shown in Erdenet time."""
    if value is None:
        return ""
    if value.tzinfo is None:  # SQLite returns timestamps without a timezone
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(OFFICE_TZ).strftime("%Y-%m-%d %H:%M")


def register_filters(app):
    app.add_template_filter(mnt)
    app.add_template_filter(mn_date)
    app.add_template_filter(hm)
    app.add_template_filter(mn_datetime)
