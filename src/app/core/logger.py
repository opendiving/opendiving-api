import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

LOG_FILE_PATH = os.path.join(LOG_DIR, "app.log")

LOGGING_LEVEL = logging.INFO
LOGGING_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

logging.basicConfig(level=LOGGING_LEVEL, format=LOGGING_FORMAT)

file_handler = RotatingFileHandler(LOG_FILE_PATH, maxBytes=10485760, backupCount=5)
file_handler.setLevel(LOGGING_LEVEL)
file_handler.setFormatter(logging.Formatter(LOGGING_FORMAT))

logging.getLogger("").addHandler(file_handler)

# NOTE: nothing imports this module. It is the app's logging configuration on paper only -
# `app.log` in this directory is a leftover from whenever it last was imported. Anything
# that has to actually hold, therefore, cannot live here: `core.setup` pins the httpx logger
# to WARNING so that `GEOCODER_API_KEY` never reaches a log, precisely because putting that
# line in this file would have guarded nothing. Wire this module up or delete it, but don't
# add safety-critical configuration to it while it is neither.
