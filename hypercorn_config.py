# Hypercorn config, loaded via: hypercorn --config file:hypercorn_config.py
#
# Hypercorn creates its Logger lazily, after the app module has imported and
# the lifespan startup has run. With the old `--log-config log_config.ini`
# that meant fileConfig re-applied root at INFO and clobbered whatever
# main.py had configured — debug/error levels set via LOG_LEVEL never took
# effect at request time. Feeding the same dict used by main.py through
# logconfig_dict makes hypercorn's late application a no-op.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from log_config import build_logging_config

logconfig_dict = build_logging_config()
