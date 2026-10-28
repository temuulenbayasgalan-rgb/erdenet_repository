"""Application settings.

Secrets come from environment variables (or a local .env file), never from code.
"""
import os


class BaseConfig:
    SECRET_KEY = os.environ.get("SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL") or None
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Session cookie hardening
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_HTTPONLY = True

    # Forms are protected against CSRF by Flask-WTF.
    WTF_CSRF_ENABLED = True

    # The office phone shown on the public job board ("Оффис руу залгах").
    # Set OFFICE_PHONE in .env, e.g. OFFICE_PHONE=7035xxxx. No default: if it is
    # missing the board says so instead of showing a broken or invented number.
    OFFICE_PHONE = os.environ.get("OFFICE_PHONE", "")

    # Public map. Pins outside MAP_BOUNDS (Erdenet / Orkhon area) are rejected.
    MAP_CENTER = (49.0275, 104.0447)
    MAP_ZOOM = 13
    MAP_BOUNDS = ((48.80, 103.60), (49.25, 104.60))  # (south, west), (north, east)
    # OpenStreetMap's public tiles: free, no key, but only for light use. See README.
    MAP_TILE_URL = os.environ.get("MAP_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
    MAP_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    # A fixed key is acceptable only on a developer's own machine.
    SECRET_KEY = os.environ.get("SECRET_KEY") or "dev-only-insecure-key"


class TestingConfig(BaseConfig):
    TESTING = True
    SECRET_KEY = "test-key"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    # Disabled so tests can post forms directly; a dedicated test re-enables it.
    WTF_CSRF_ENABLED = False


class ProductionConfig(BaseConfig):
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True


CONFIGS = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
