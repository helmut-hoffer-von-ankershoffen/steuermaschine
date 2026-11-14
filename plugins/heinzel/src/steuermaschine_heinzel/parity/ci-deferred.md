# Parity: `sm ci deferred` against `--list` of the former deferral script

Same lines (`id branch workflow name`, oldest first), same empty output for an empty list. The list comes from the
store; until a deferral or re-run here has written it, the former list file is read (read-only), so the first call
after the switch shows what the old tool recorded. Compared with a written old-format file in
`test_the_former_list_is_read_until_the_store_has_one`. json: one `Deferred` document.
