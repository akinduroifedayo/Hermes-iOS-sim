IOS_SETUP_SCHEMA = {
    "name": "ios_setup",
    "description": "Read-only iOS prerequisite preflight or host-approved pinned stock dependency provisioning. Does not build, install Apple tools, or start a simulator/server.",
    "parameters": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["preflight", "provision", "status"]},
            "worktree": {"type": "string", "description": "Authorized local worktree; required for preflight only."},
        },
        "required": ["action"],
        "allOf": [{"if": {"properties": {"action": {"const": "preflight"}}}, "then": {"required": ["worktree"]}}],
    },
}

IOS_ITERATION_SCHEMA = {
    "name": "ios_iteration",
    "description": (
        "Resolve and operate the exact local iOS Simulator runtime for a native iOS project. "
        "Optional action='run' coordinates build/install/launch; ordinary Xcode/simctl or available native tooling is also supported. Use action='preview' after external launch to start/reuse stock for the exact booted Simulator without building or issuing a native receipt. "
        "Discover/preflight first; run returns exact target, receipt, separate native/server outcomes, and preview_url/preview_action only for a ready server. "
        "Load the bundled ios-development skill for the complete workflow. On successful run or preview, immediately call PUBLIC desktop_preview(action='open', url=preview_url) in the same originating agent turn before replying; never finish with only a URL. "
        "Successful current run/preview automatically prepares app_card. After the public open call, include app_card.view.directive in the reply by default when app_card.status=prepared; report unavailable app_card.code separately without changing native success. "
        "This is agent choreography, not an atomic callback or visible-pixel proof. If preview opening fails/unavailable, report it separately from native success. "
        "User feedback uses host Browser annotation and ordinary Send; inspect the exact submitted image with vision_analyze before feedback-driven edits. "
        "Explicit rebuild reuses the same URL/target; status polling must never reopen a closed preview. "
        "Approved run/preview durably claims device coordination for the authenticated conversation. Cancel/stop require the exact target and expected_claim_generation from claim or runtime.claim in status. Only the current owner can host-approve handoff to recipient_session in the same profile. "
        "Action card issues (card_action=run|preview) or resolves (card_id) a session-bound file widget; its send button requests chat review and never grants approval or proves fresh pixels. "
        "Do not use for ordinary code editing, explanations, code review, Android/web work, or tests that do not request an app launch."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["discover", "preflight", "run", "preview", "status", "cancel", "stop", "handoff", "card"]},
            "worktree": {"type": "string", "description": "Absolute canonical local worktree path."},
            "card_action": {"type": "string", "enum": ["run", "preview"], "description": "Card only. Issue an observational Build and run/Open app request."},
            "card_id": {"type": "string", "pattern": "^[0-9a-f]{32}$", "description": "Opaque card lookup in the authenticated session; card resolves, run/preview consumes only after normal approval."},
            "project": {"type": "string", "description": "In-worktree .xcodeproj or .xcworkspace path."},
            "scheme": {"type": "string"},
            "configuration": {"type": "string"},
            "runtime": {
                "type": "string",
                "pattern": r"^iOS (?:Simulator|\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)$",
                "description": "Exact selected iOS Simulator runtime, such as 'iOS 26.5'. Use 'iOS Simulator' only when the target is intentionally version-agnostic.",
            },
            "udid": {"type": "string", "description": "Exact selected Simulator UDID."},
            "expected_claim_generation": {"type": "integer", "minimum": 0,
                "description": "Claim generation returned by run, preview or status. Required for cancel, stop and handoff."},
            "recipient_session": {"type": "string", "minLength": 1,
                "description": "Handoff only. Recipient session in this profile. Must be requested and host-approved by the current owner."},
        },
        "required": ["action", "worktree"],
        "allOf": [{"if": {"properties": {"action": {"enum": ["run", "preview", "status", "cancel", "stop", "handoff", "card"]}}},
                   "then": {"required": ["project", "scheme", "configuration", "runtime", "udid"]}},
                  {"if": {"properties": {"action": {"enum": ["cancel", "stop", "handoff"]}}},
                   "then": {"required": ["expected_claim_generation"]}},
                  {"if": {"properties": {"action": {"const": "handoff"}}},
                   "then": {"required": ["recipient_session"]}}],
    },
}
