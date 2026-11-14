"""Names vulture (bin/check, dead code) reports that are used: by a framework through reflection, or inside an approved
review unit or a moved library that may not change without a new review. Each line says which. Never imported."""
# ruff: noqa: B018, F821

# pydantic-settings calls settings_customise_sources with these keywords; the override must accept them
dotenv_settings, file_secret_settings

# approved review unit head-validate (native/window.py): a signal handler's required parameters
signum, frame

# approved review unit head-ship (native/preflight_lib/head.py) and the queue unit (native/queue_lib/flakes_read.py),
# moved verbatim
chained, defaultdict
