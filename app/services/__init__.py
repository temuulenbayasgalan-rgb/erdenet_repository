"""Business rules that span several models, kept out of the route functions."""


class ServiceError(Exception):
    """A business rule was broken. The message is Mongolian and safe to show to the user."""
