# Historical stock-runtime fixtures

These files retain observations of `serve-sim@0.1.48` captured during development. They are test inputs, not a live security pass or installed-candidate acceptance.

`tests/integration/stock-runtime/host-contract.test.ts` asserts the recorded HTTP statuses, WebSocket handshake outcomes, and package-provenance fields. HTTP and WebSocket tests sent no shell command.

## Privacy normalization

- `stock-provenance.json` omits the original absolute npm-cache path.
- `security-probes.json` replaces the simulator identifier in the helper WebSocket route with the synthetic `00000000-0000-4000-8000-000000000104`.
- Loopback origins and ports are historical observations, not externally reachable host addresses.
- Other response and provenance fields retain their recorded values. Registry signatures and integrity hashes are public package metadata, not credentials.

The helper-route identifier is therefore not raw historical evidence. The private original remains outside this distribution. No test depends on that identifier being a real simulator.

Unit and process fixtures use synthetic device identifiers. The `00000000-0000-4000-8000-0000000001xx` family replaces development identifiers. Qualification device selection must respect the operator’s current device permissions; the receipt verifier does not embed a developer-machine blacklist.
