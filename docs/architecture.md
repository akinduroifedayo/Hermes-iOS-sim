# Architecture and iteration flow

The agent edits actual source files. The plugin coordinates Apple tooling and a preview service. Hermes owns the Browser pane, annotations, and image inspection.

## Components

```mermaid
flowchart TD
    User[Request or annotation] --> Agent[Hermes agent]
    Agent --> Files[Swift project files]
    Agent --> Setup[ios_setup]
    Agent --> Iteration[ios_iteration]
    Setup --> Dependency[Locked serve-sim dependency]
    Files --> Xcode[xcodebuild and simctl]
    Iteration --> Xcode
    Xcode --> Simulator[Running Simulator app]
    Iteration --> Service[Owned serve-sim service]
    Dependency --> Service
    Simulator -->|Pixels| Service
    Service -->|Stream| Browser[Hermes Browser]
    Browser -->|Input| Service
    Service -->|Device input| Simulator
    Iteration --> State[Claims, operation records and leases]
    State --> Watchdog[Lifecycle watchdog]
    Browser -->|Annotation and ordinary Send| User
```

## `ios_iteration` actions

```mermaid
flowchart TD
    Call[Tool request] --> Action{Action}
    Action -->|discover| Discover[Find projects, schemes and simulators]
    Action -->|preflight| Preflight[Check prerequisites]
    Action -->|status| Status[Read operation and preview status]
    Action -->|card| Card[Issue or resolve a session-bound card]
    Action -->|run or preview| Approval[Host approval and exact-target validation]
    Approval --> Ownership[Conversation claim and target serialization]
    Ownership --> Mode{Mode}
    Mode -->|run| Build[Build]
    Build --> Install[Install]
    Install --> Launch[Launch]
    Launch --> Preview[Start or reuse preview service]
    Mode -->|preview| Booted[Verify simulator is already booted]
    Booted --> Preview
    Preview --> Ready{Readiness and identity checks}
    Ready -->|Pass| Result[Return outcomes, preview URL and app card]
    Ready -->|Fail| Error[Return failure details]
    Build -->|Failure| Error
    Install -->|Failure| Error
    Launch -->|Failure| Error
    Result --> Open[Agent calls desktop_preview open]
    Open --> Verify[Observe pixels and test behavior]
    Action -->|cancel or stop| Owned[Validate owning conversation and claim]
    Owned --> Cleanup[Cancel owned work or stop owned resources]
    Action -->|handoff| Transfer[Approved transfer to another conversation]
```

`run` builds, installs, and launches. `preview` does none of those and does not boot a simulator. Both can return a preview URL, but the agent must call the separate `desktop_preview` host tool to open the pane.

Source editing happens outside `ios_iteration`. Cards send a request into chat; a card click does not bypass normal approval. Stop can terminate a coordinator-owned app as well as its preview service.

## Results that must stay separate

A successful native build does not establish preview readiness. Preview readiness does not establish displayed pixels. Displayed pixels do not establish that input reaches the app. An unchanged URL does not establish uninterrupted server lifetime.

See [operations](operations.md) for action parameters, lifecycle behavior, and failure codes. These diagrams are conceptual maps, not exhaustive call graphs.
