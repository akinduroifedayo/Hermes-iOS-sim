# Reconcile a prior-boot legacy stock record

This is an operator maintenance command, not automatic recovery and not an agent tool. It is for a stock generation recorded before canonical process-birth evidence was available. Normal cleanup must continue to refuse ambiguous legacy PID reuse.

Use it only when an operator has independently retrieved the original `ios_iteration` ready-preview tool result and its storage-layer metadata, and established that it came from this Mac. A conversation summary, a copied claim, a `first_stopped` diagnostic, changed process text, or an unverified receipt is not sufficient. The historical receipt contains no hardware identity. The command records the operator's same-host assertion explicitly rather than inventing one.

## Required evidence

Export the original tool row as JSON with `id`, `session_id`, `tool_call_id`, `role`, `tool_name`, `timestamp`, and the exact raw `content` string. `content_sha256` may accompany it. Pin both the SHA-256 of the complete export and the SHA-256 of raw content. The whole-export pin binds the timestamp and locator metadata as well as the result.

Independently establish the Mac's `IOPlatformUUID` and current `kern.bootsessionuuid`. Verify the actual reboot history, not just a computed boot time: large wall-clock steps can make epochs incomparable. The receipt must predate the current kernel boot by more than five minutes. This command deliberately cannot reconcile a same-boot legacy record. It does not change the clock assumptions of ordinary runtime cleanup.

## Run

Run the installed module through the normal command approval path, using the actual paths and independently verified values:

```sh
python3 -I -B "$PLUGIN_ROOT/legacy_recovery.py" \
  --state "$PLUGIN_DATA/stock-lifecycle.json" \
  --receipt "$VERIFIED_EXPORT" \
  --expected-receipt-sha256 "$EXPORT_SHA256" \
  --expected-content-sha256 "$CONTENT_SHA256" \
  --expected-generation "$GENERATION" \
  --expected-host-uuid "$HOST_UUID" \
  --expected-boot-session "$BOOT_UUID"
```

The default is dry-run. Check `status: would_apply`, the exact generation, and every obligation in the returned proof. Repeat the same command with `--apply` only after reviewing that result. The command rechecks evidence rather than trusting the earlier dry-run.

It takes the existing bounded device and store locks, requires unchanged root and descendant attestations, reads a fresh canonical process table, and rechecks boot identity. Root presence, an old-group member, unknown process state, insufficient birth margin, malformed data, a changed generation or an incomplete observation refuses recovery without rewriting the record. Nothing sends a signal, removes an ownership obligation, changes a profile configuration, or erases app data.

A successful apply appends the existing terminal-proof schema and separate `legacy_recovery` provenance. It preserves the original record, all identity fields, all descendants and all unrelated state. Read back that exact record before calling the normal installed preview action. The preview still uses ordinary host approval and normal server/port checks. A successful maintenance result is not a successful preview or full product acceptance.

Repeated application returns `already_proved` without rewriting an already valid proof. A malformed existing proof is not overwritten. A dry-run may create the ordinary lock files but does not rewrite lifecycle JSON.
