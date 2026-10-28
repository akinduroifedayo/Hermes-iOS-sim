"""Pure HTML presentation for the host-owned preview file widget.

The send attribute requests a chat turn; it supplies no execution authority.
"""

from __future__ import annotations

from html import escape

from .card_requests import CardRequest, card_prompt, card_reusable, card_sendable


def render_card_widget(
    card: CardRequest, *, app: str, project: str, scheme: str, device: str, result_text: str = "",
    runtime: str = "", status_text: str | None = None,
) -> str:
    """Render a validated record and exact trusted display strings."""
    label = "Build and run" if card.action == "run" else "Open app"
    if card_sendable(card):
        button_attrs = f'data-hermes-send="{escape(card_prompt(card), quote=True)}"'
        explanation = (
            "Reopens or reveals this device's live app. Each use is reviewed in chat."
            if card_reusable(card) else "Sends this card request to the chat for review."
        )
    else:
        button_attrs = "disabled"
        explanation = (
            "This card request has been consumed and cannot be sent again."
            if card.status == "consumed"
            else "This card request has been retired and is no longer available."
        )
    if status_text is None:
        status_text = ("Ready to open" if card.action == "preview" and card.status == "pending"
            else "Last Preview operation outcome unknown" if card_reusable(card)
            else "Ready for review" if card.status == "pending" else "Request " + card.status)
    summary = "\n".join(
        f"<dt>{name}</dt><dd>{escape(value, quote=True)}</dd>"
        for name, value in (("App", app), ("Project", project), ("Scheme", scheme), ("Device", device))
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(app, quote=True)}</title>
<style>
.card {{ color: var(--foreground); background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; max-width: 40rem; }}
.card h1 {{ font-size: 1.125rem; line-height: 1.3; margin: 0 0 4px; overflow-wrap: anywhere; }}
.card .target {{ margin: 0 0 12px; }}
.card .state {{ margin: 0 0 12px; color: var(--foreground); }}
.card p, .card summary {{ overflow-wrap: anywhere; }}
.card details {{ margin-top: 16px; font-size: 0.875rem; }}
.card summary {{ cursor: pointer; color: var(--muted-foreground); }}
.card ::selection {{ background: var(--accent); color: var(--foreground); }}
.card dl {{ display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px 16px; }}
.card dt, .card p {{ color: var(--muted-foreground); }}
.card dd {{ margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }}
.card button {{ color: var(--foreground); background: var(--accent); border: 1px solid var(--border); border-radius: 6px; padding: 10px 16px; font: inherit; cursor: pointer; }}
.card button:focus-visible, .card summary:focus-visible {{ outline: 3px solid var(--accent); outline-offset: 3px; }}
.card button:hover:not(:disabled) {{ background: var(--card); }}
.card button:disabled {{ opacity: 0.6; cursor: default; }}
</style>
</head>
<body>
<main class="card">
<h1>{escape(app, quote=True)}</h1>
<p class="target"><span>{escape(scheme, quote=True)}</span> <span>{escape(runtime, quote=True)}</span></p>
<p class="state">{escape(status_text, quote=True)}</p>
<button type="button" aria-describedby="card-status" {button_attrs}>{label}</button>
<p id="card-status">{explanation}</p>
<details>
<summary>Details</summary>
<dl>
{summary}
</dl>
<p>{escape(result_text, quote=True)}</p>
</details>
</main>
</body>
</html>
"""
