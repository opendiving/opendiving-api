from enum import StrEnum


class MapTheme(StrEnum):
    """The basemap a map picture is drawn in: the web app's resolved colour scheme."""

    LIGHT = "light"
    DARK = "dark"
