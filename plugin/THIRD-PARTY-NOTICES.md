# Third-party dependency notices

The plugin archive contains dependency manifests and a lockfile, not the dependencies or native executables. Approved setup downloads those files separately. This inventory describes the locked tree in `dependencies/package-lock.json`; it does not grant rights beyond each upstream license.

| Package | Locked version | Declared license |
| --- | --- | --- |
| serve-sim | 0.1.49-beta.139.1 | Apache-2.0 |
| inspect-webkit | 0.0.5 | MIT |
| react | 19.3.0 | MIT |
| react-dom | 19.3.0 | MIT |
| scheduler | 0.28.0 | MIT |
| sonner | 2.0.8 | MIT |
| typescript | 5.9.3 | Apache-2.0 |
| ws | 8.22.0 | MIT |

The runtime `serve-sim` pin is exactly 0.1.49-beta.139.1. In this plugin configuration it requires a loopback Host, leaves shell execution disabled (no `--allow-exec`), and does not put the execution token in preview HTML. Its npm metadata names [EvanBacon/serve-sim](https://github.com/EvanBacon/serve-sim), package directory `packages/serve-sim`, and source revision `0193e65c481fc9b64a62427f32fcf143dbc3d2c0`. Metadata is not independent provenance verification.

The [upstream Apache license at that revision](https://github.com/EvanBacon/serve-sim/blob/0193e65c481fc9b64a62427f32fcf143dbc3d2c0/LICENSE) is copied byte-for-byte in [licenses/serve-sim-APACHE-2.0.md](licenses/serve-sim-APACHE-2.0.md), including its Evan Bacon copyright attribution. The copy comes from that pinned source revision, not from the npm archive. The complete GitHub source-tree listing at that revision contains no NOTICE file; no upstream NOTICE has been invented or added.

The downloaded 0.1.49-beta.139.1 npm archive contains no file named LICENSE, NOTICE, or COPYING. Other packages in the locked tree contain license files; TypeScript also contains `ThirdPartyNoticeText.txt`. Setup preserves files shipped in the archives rather than removing their notices.

The `serve-sim` archive includes a native `.node` addon, camera dylib and helper, accessibility helper, and HID/render helpers. Registry integrity checks establish agreement with published bytes. They do not establish reproducible native builds, complete linked-component licensing, or permission to redistribute Apple components.

Do not add those native files to the plugin archive without a separate license and redistribution review. That review, including any applicable NOTICE obligations and linked native components, remains incomplete. The dependency's Apache declaration alone is not a complete native redistribution audit. Xcode and Simulator runtimes are user-installed prerequisites and are not distributed here.

The Desktop bundle externalizes `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime`; the host supplies them. Build-time Bun, TypeScript, and Bun type definitions are not vendored in the plugin package.

Original project code is MIT-licensed under `LICENSE.md`, copyright 2026 Hermes-iOS contributors. The MIT grant excludes third-party materials. `serve-sim` remains Apache-2.0, and other third-party materials retain their own licenses and notices. This component-wise decision is not whole-project dual licensing and does not close the native redistribution review.
