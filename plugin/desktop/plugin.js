// src/desktop/plugin.js
import { PALETTE_AREA, host as host2 } from "@hermes/plugin-sdk";
import { jsx as jsx2 } from "react/jsx-runtime";

// src/desktop/runtime-status-view.js
import { jsx, jsxs } from "react/jsx-runtime";
import { useState, useSyncExternalStore } from "react";
import { host, useValue, useQuery, Button, Input } from "@hermes/plugin-sdk";

// src/desktop/runtime-view-model.ts
function nativeStatus(runtime) {
  const active = { building: "Building app", booting: "Preparing simulator", installing: "Installing app", launching: "Launching app" };
  const recovery = { building: "Fix compiler diagnostics, then ask Hermes to rebuild this target.", resolve_artifact: "Check the selected application product and build settings.", booting: "Check the selected simulator runtime and device availability.", installing: "Check install diagnostics; retry this target without erasing app data.", verify_install: "Check the installed executable before retrying.", launching: "Check launch diagnostics, then retry this target.", verify_pid: "Check launch process verification before retrying.", server: "Check prerequisites and stock server diagnostics, then explicitly retry." };
  const failure = runtime?.last_failure;
  const phase = runtime?.phase ?? "";
  const stage = failure?.evidence?.stage ?? "";
  const title = active[phase] ?? (failure ? `${stage || "Run"} failed` : runtime?.server?.status === "ready" ? "Server ready" : runtime?.server?.status === "stopped" ? "Runtime stopped" : runtime?.server?.status === "crashed" ? "Server unavailable" : "Choose exact target");
  return {
    title,
    detail: [
      failure?.evidence?.detail ?? failure?.error,
      failure ? recovery[stage] ?? "Check diagnostics, then explicitly retry this target." : null,
      runtime?.last_success ? `Last successful app: ${runtime.last_success.build_attempt_id}` : "No successful app recorded",
      `Server: ${runtime?.server?.status ?? "absent"}`,
      "Pixels: unknown"
    ].filter(Boolean).join(" · ")
  };
}

// src/desktop/runtime-status-view.js
function NativeControls({ client }) {
  const profile = useValue(host.state.profile);
  const cwd = useValue(host.state.cwd);
  const gateway = useValue(host.state.gateway);
  const [selection, setSelection] = useState(null);
  const [discovery, setDiscovery] = useState(null);
  const [prerequisites, setPrerequisites] = useState(null);
  const [error, setError] = useState(null);
  const [pending, setPending] = useState(null);
  const [worktreeInput, setWorktree] = useState(null);
  const scope = useSyncExternalStore(client.subscribe, client.snapshot);
  const worktree = worktreeInput?.scope === scope ? worktreeInput.value : cwd;
  const current = selection?.scope === scope ? selection.target : null;
  const status = useQuery({
    queryKey: ["hermes-ios", profile, cwd, gateway, scope, current],
    enabled: Boolean(current),
    refetchInterval: 3000,
    queryFn: () => {
      if (client.snapshot() !== scope)
        throw new Error("native_scope_retired");
      return client.request({ ...current, action: "status" });
    }
  });
  const presentation = nativeStatus(status.data?.runtime);
  async function action(name) {
    const operationScope = client.snapshot();
    if (operationScope !== scope)
      return;
    setPending({ scope: operationScope });
    setError(null);
    try {
      const result = await client.request(name === "discover" || name === "preflight" ? { action: name, worktree } : { ...current, action: name });
      if (client.snapshot() !== operationScope)
        return;
      if (name === "discover")
        setDiscovery({ scope: operationScope, result });
      if (name === "preflight")
        setPrerequisites({ scope: operationScope, result });
      if (name === "cancel" || name === "stop")
        await status.refetch();
    } catch (failure) {
      if (client.snapshot() === operationScope && !String(failure).includes("native_scope_retired"))
        setError({ scope: operationScope, message: String(failure) });
    } finally {
      if (client.snapshot() === operationScope)
        setPending(null);
    }
  }
  const choices = [];
  const found = discovery?.scope === scope ? discovery.result : null;
  for (const target of found?.targets ?? [])
    for (const configuration of target.configurations ?? []) {
      for (const device of found.devices ?? [])
        if (target.compatible_udids.includes(device.udid)) {
          choices.push({
            worktree: found.worktree,
            project: target.project,
            scheme: target.scheme,
            configuration,
            runtime: device.runtime,
            udid: device.udid
          });
        }
    }
  const button = (label, name, disabled = false) => jsx(Button, {
    size: "sm",
    disabled: pending?.scope === scope || disabled,
    onClick: () => void action(name),
    children: label
  });
  return jsxs("section", { "aria-label": "iOS native controls", style: {
    padding: 12,
    display: "grid",
    gridTemplateColumns: "minmax(0, 1fr)",
    gap: 12,
    color: "var(--ui-text-primary)",
    overflow: "auto",
    overflowWrap: "anywhere",
    maxHeight: "100%"
  }, children: [
    jsx("strong", { children: "iOS target and run status" }),
    jsx("label", { children: jsxs("span", { children: ["Authorized worktree", jsx(Input, {
      value: worktree,
      onChange: (event) => {
        if (client.snapshot() !== scope)
          return;
        client.retire();
        setWorktree({ scope: client.snapshot(), value: event.target.value });
      },
      "aria-label": "Authorized worktree"
    })] }) }),
    jsxs("div", { style: { display: "flex", gap: 8, flexWrap: "wrap" }, children: [button("Discover targets", "discover"), button("Check prerequisites", "preflight")] }),
    choices.length ? jsx("select", {
      "aria-label": "Exact iOS target",
      value: current ? JSON.stringify(current) : "",
      onChange: (event) => {
        if (client.snapshot() !== scope)
          return;
        client.retire();
        const nextScope = client.snapshot();
        setDiscovery({ scope: nextScope, result: found });
        setWorktree({ scope: nextScope, value: worktree });
        setSelection({ scope: nextScope, target: JSON.parse(event.target.value) });
      },
      style: { maxWidth: "100%", color: "var(--ui-text-primary)", background: "var(--ui-bg-primary)" },
      children: [
        jsx("option", { disabled: true, value: "", children: "Choose project / scheme / configuration / device" }),
        ...choices.map((target) => jsx("option", { value: JSON.stringify(target), children: `${target.project} · ${target.scheme} · ${target.configuration} · ${target.runtime} · ${target.udid}` }, JSON.stringify(target)))
      ]
    }) : null,
    current ? jsx("div", { style: { overflowWrap: "anywhere" }, children: `${current.project} · ${current.scheme} · ${current.configuration} · ${current.runtime} · ${current.udid}` }) : null,
    jsx("div", { role: "status", "aria-live": "polite", children: jsxs("div", { children: [
      jsx("strong", { children: presentation.title }),
      jsx("p", { children: presentation.detail })
    ] }) }),
    jsx("p", { children: "Ask Hermes in chat to build and run this exact target. Preview and annotations use Hermes Browser; this panel does not open it." }),
    jsxs("div", { style: { display: "flex", gap: 8 }, children: [button("Cancel operation", "cancel", !current), button("Stop owned runtime", "stop", !current)] }),
    error?.scope === scope || status.error ? jsx("p", { role: "alert", style: { overflowWrap: "anywhere" }, children: error?.scope === scope ? error.message : String(status.error) }) : null,
    prerequisites?.scope === scope ? jsxs("details", { children: [
      jsx("summary", { children: prerequisites.result.ready ? "Prerequisites ready" : "Prerequisites need attention" }),
      jsx("pre", { style: { whiteSpace: "pre-wrap", overflowWrap: "anywhere" }, children: JSON.stringify(prerequisites.result, null, 2) })
    ] }) : null,
    found?.diagnostics?.length ? jsx("pre", { style: { whiteSpace: "pre-wrap" }, children: JSON.stringify(found.diagnostics, null, 2) }) : null
  ] });
}

// src/desktop/runway-session.js
class NativeControlSession {
  generation = 0;
  key = null;
  observe(scope) {
    const key = JSON.stringify(scope);
    if (key !== this.key) {
      this.key = key;
      this.generation += 1;
    }
  }
  capture(scope) {
    this.observe(scope);
    return this.generation;
  }
  accept(scope, token) {
    this.observe(scope);
    return token === this.generation;
  }
  retire() {
    this.key = null;
    this.generation += 1;
  }
}

// src/desktop/runtime-client.ts
class NativeRuntimeClient {
  options;
  session = new NativeControlSession;
  disposed = false;
  listeners = new Set;
  snapshot = () => `${this.session.key}:${this.session.generation}`;
  subscribe = (listener) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  publish(previous) {
    if (previous !== this.snapshot())
      for (const listener of this.listeners)
        listener();
  }
  retire() {
    const previous = this.snapshot();
    this.session.retire();
    this.session.observe(this.options.scope());
    this.publish(previous);
  }
  dispose() {
    this.disposed = true;
    this.retire();
    this.listeners.clear();
  }
  observe() {
    if (this.disposed)
      return;
    const previous = this.snapshot();
    this.session.observe(this.options.scope());
    this.publish(previous);
  }
  constructor(options) {
    this.options = options;
  }
  async request(body) {
    if (!["discover", "preflight", "status", "cancel", "stop"].includes(String(body.action)))
      throw new Error("unsupported_panel_action");
    if (this.disposed)
      throw new Error("native_scope_retired");
    const scope = this.options.scope();
    const token = this.session.capture(scope);
    if (scope.mode !== "local" || !scope.connectionId || !scope.targetProfile)
      throw new Error("local_topology_required");
    if (this.disposed || !this.session.accept(this.options.scope(), token))
      throw new Error("native_scope_retired");
    const result = await this.options.rest("/runtime", { method: "POST", body });
    if (this.disposed || !this.session.accept(this.options.scope(), token))
      throw new Error("native_scope_retired");
    return result;
  }
}

// src/desktop/plugin.js
var plugin_default = {
  id: "hermes-ios",
  name: "Hermes-iOS-sim",
  defaultEnabled: false,
  register(ctx) {
    let route = null;
    let revision = 0;
    let disposed = false;
    const active = () => ({
      profile: host2.state.profile.get(),
      worktree: host2.state.cwd.get(),
      connectionId: host2.state.connectionId.get(),
      gateway: host2.state.gateway.get()
    });
    let resolvedFor = null;
    const scope = () => {
      const current = active();
      const valid = resolvedFor === JSON.stringify(current);
      return {
        ...current,
        targetProfile: valid ? route?.targetProfile ?? null : null,
        mode: valid ? route?.mode ?? "unknown" : "unknown"
      };
    };
    const client = new NativeRuntimeClient({ scope, rest: (path, options) => ctx.rest(path, options) });
    const refresh = async () => {
      const ticket = ++revision;
      const captured = active();
      route = null;
      resolvedFor = null;
      client.retire();
      client.observe();
      try {
        const routes = await host2.profileRoutes();
        if (disposed || ticket !== revision || JSON.stringify(active()) !== JSON.stringify(captured))
          return;
        const matches = Array.isArray(routes) ? routes.filter((value) => value.profile === captured.profile && value.connectionId === captured.connectionId) : [];
        const candidate = matches.length === 1 ? matches[0] : null;
        if (candidate?.mode === "local" && typeof captured.connectionId === "string" && captured.connectionId && typeof candidate.targetProfile === "string" && candidate.targetProfile)
          route = candidate;
        resolvedFor = JSON.stringify(captured);
        client.observe();
      } catch {}
    };
    ctx.onDispose(() => {
      disposed = true;
      ++revision;
      route = null;
      resolvedFor = null;
      client.dispose();
    });
    for (const state of [host2.state.profile, host2.state.cwd, host2.state.gateway, host2.state.connectionId]) {
      ctx.onDispose(state.subscribe(() => {
        if (!disposed)
          refresh();
      }));
    }
    let closeControls = null;
    ctx.onDispose(() => {
      closeControls?.();
      closeControls = null;
    });
    ctx.register({
      id: "open-controls",
      area: PALETTE_AREA,
      data: {
        id: "hermes-ios.open-controls",
        label: "Open Hermes-iOS-sim controls",
        keywords: ["ios", "native", "controls"],
        run: () => {
          if (disposed)
            return;
          if (typeof host2.openWorkspace !== "function") {
            host2.notify({ kind: "error", message: "Update Hermes Desktop to open Hermes-iOS-sim controls." });
            return;
          }
          closeControls = host2.openWorkspace("hermes-ios:controls", {
            title: "Hermes-iOS-sim",
            render: () => jsx2(NativeControls, { client }),
            onClose: () => {
              closeControls = null;
            }
          });
        }
      }
    });
  }
};
export {
  plugin_default as default
};
