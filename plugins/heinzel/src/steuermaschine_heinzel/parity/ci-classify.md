# ci classify (write: sends data to an outside service, counts a budget file)

Compared with the old log tool's optional class line (its tests, moved to `tests/test_native_ci_classify_jev.py`,
16 cases against a fake service on the loopback interface: bearer key and typed answer, default off and kill
switch, no key, allowlist, retry after 429, server errors, 401 pause, oversized state, budget caps, low confidence,
redaction, remote http refused).

| Input | Old (log tool with the environment switch on) | `sm ci classify RUN` |
|---|---|---|
| a confident answer | a line `jev: timeout p=0.95` under the condensed log | `classifier: timeout p=0.95`, then what was sent and the budget of the day |
| switch off, kill switch, other repository | no line, silently | refuses with the reason; exit 1 |
| low confidence, service error, no key | no line | `classifier: no confident class (or no answer)`; exit 0 |

Side effects, in this order: one GitHub read of the failed-step log; the key read (settings, else one `security
find-generic-password` call); the budget file `budget.json` counted up under `budget.lock`; one to three POSTs to the
service; on a rejected key the file `disabled-until`. Nothing else is written.

Differs on purpose: it is its own command instead of a side effect of a read; the switch is the setting
`ci_classifier` (default off) instead of an environment variable; the keychain item is read only inside this command.
