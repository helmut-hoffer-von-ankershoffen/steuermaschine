"""Logic of the CI reads: cause.py (why a job failed), faillog.py (which failing check a rerun names); the log cutting
and the failing test names are the core's (`steuermaschine.github.logs`). The registry of known failures is read with
the queue slice's library (queue_lib/known_failures.py). Nothing here sends anything outside this machine but GitHub
reads."""
