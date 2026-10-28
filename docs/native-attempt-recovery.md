# Native attempt recovery

`python3 -m plugin.native_attempt_recovery` is an explicit operator command. It finalizes one retained install-cancelled attempt as `recovered_native_dispatch_failure`. It has no runtime hook and never invokes native tools, inspects processes, sends signals, or discharges cleanup obligations.

Supply every path explicitly. The target file contains the expected `RuntimeIdentity` JSON object, including its target fingerprint and domain key. The receipt file contains the operator-verified original failed `ios_iteration` tool row. Its raw `content` must report `ok=false`, `error=native_dispatch_failed`, and the matching operation ID and domain key. Receipt metadata requires `id`, `session_id`, `tool_call_id`, `role=tool`, `tool_name=ios_iteration`, and a positive, nonfuture storage `timestamp`.

The version 1 manifest has exactly these fields:

```json
{
  "version": 1,
  "state": "<absolute resolved lifecycle JSON path>",
  "operation_lock_root": "<absolute corresponding backend operation lock directory>",
  "state_sha256": "<SHA-256 of exact lifecycle file bytes>",
  "evidence_sha256": "<SHA-256 of canonical retained attempt evidence>",
  "identity": {"<RuntimeIdentity fields>": "<expected values>"},
  "operation_id": "operation:<id>",
  "build_attempt_id": "build:<id>",
  "receipt_sha256": "<SHA-256 of exact receipt file bytes>",
  "content_sha256": "<SHA-256 of the receipt content string encoded as UTF-8>"
}
```

Canonical evidence is the selected `v1_native_obligations[build_attempt_id]` serialized with Python `json.dumps(evidence, sort_keys=True, separators=(',', ':'), allow_nan=False)` and encoded as UTF-8. Digests are lowercase hexadecimal without a prefix. Pin the exact manifest file bytes separately with `--expected-manifest-sha256`.

```sh
python3 -m plugin.native_attempt_recovery \
  --state "$STATE" --operation-lock-root "$OPERATION_LOCK_ROOT" \
  --target "$TARGET" --operation-id "$OPERATION_ID" \
  --build-attempt-id "$BUILD_ATTEMPT_ID" \
  --receipt "$RECEIPT" --manifest "$MANIFEST" \
  --expected-manifest-sha256 "$MANIFEST_SHA256"
```

The default reports `would_apply`. Add `--apply` for `applied`. Success returns exit 0. Refusal returns exit 2 and a reason code. The same manifest and unchanged recovered snapshot report `already_recovered` without rewriting state. Any conflicting replay, including changed snapshot formatting, refuses.

The command uses `AuthorityBackend`'s actual operation lock, keyed by the exact UDID string, then the store's device lock, keyed by lowercase UDID, then the store lock. All acquisitions are nonblocking. The operator must provide the lock root used by the corresponding backend. An arbitrary unused lock directory cannot establish inactivity in a different backend. No lockfiles are deleted. Lock acquisition can create lockfiles and normalize owner permissions; dry-run and replay do not rewrite lifecycle JSON.

Eligibility requires a unique latest installing attempt with no native result or finish time, matching retained cancellation evidence, verified cleanup, an app that never started, and an attached simulator explicitly preserved. Missing or unknown cleanup, launch-phase evidence, success, competing attempts, active helpers, and pending cleanup refuse. The bounded command does not recover other cancellation shapes or attempts whose evidence was already removed by stop.

The normal native-result mutation is shared with the operator store method so validation and finalization remain under one store lock. Recovery copies the cancellation evidence and adds hashes and operator provenance. It preserves repair budgets, last success, other attempts and domains, retained cleanup obligations, and prior events. It makes no claim about what triggered the original dispatch failure.

The implementation session exercised synthetic files only. Commands, exit receipts, a stop-only reproduction, and CLI fixture snapshots are in `.audit`. No installed-state, native, simulator, or full product acceptance was performed.
