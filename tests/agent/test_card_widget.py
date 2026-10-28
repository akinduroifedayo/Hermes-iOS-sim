from __future__ import annotations

import builtins
import importlib.util
import sys
import unittest
from contextlib import ExitStack
from dataclasses import asdict, replace
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from plugin.card_requests import CardRequest, card_prompt
from plugin.card_widget import render_card_widget


class ParsedWidget(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


class CardWidgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.card = CardRequest(
            card_id="0123456789abcdef0123456789abcdef",
            session_id="session:private", target_fingerprint="sha256:private-target",
            source_revision="sha256:private-source", build_input_revision="sha256:private-inputs",
            action="run", action_revision="run:private-revision",
        )
        self.target = {
            "app": "Hermes", "project": "/work/Hermes.xcodeproj",
            "scheme": "Hermes Debug", "device": "iPhone 17 — exact device",
        }

    def test_both_pending_actions_have_exact_label_and_bounded_opaque_prompt(self) -> None:
        for action, label in (("run", "Build and run"), ("preview", "Open app")):
            with self.subTest(action=action):
                card = replace(self.card, action=action)
                html = render_card_widget(card, **self.target)
                parsed = ParsedWidget(html)
                buttons = [attrs for tag, attrs in parsed.elements if tag == "button"]
                self.assertEqual(len(buttons), 1)
                self.assertEqual(buttons[0]["type"], "button")
                self.assertNotIn("disabled", buttons[0])
                self.assertEqual(buttons[0]["data-hermes-send"], card_prompt(card))
                self.assertLessEqual(len(buttons[0]["data-hermes-send"]), 500)
                self.assertIn(label, parsed.text)
                for value in self.target.values():
                    self.assertIn(value, parsed.text)
                for field in ("session_id", "target_fingerprint", "source_revision",
                              "build_input_revision", "action_revision"):
                    self.assertNotIn(getattr(card, field), html)

    def test_consumed_run_and_retired_cards_disable_without_any_send_attribute(self) -> None:
        for action in ("run", "preview"):
            for status in ("consumed", "retired"):
                if action == "preview" and status == "consumed":
                    continue
                with self.subTest(action=action, status=status):
                    card = replace(self.card, action=action, status=status,
                                   consumed_request_id="request:private" if status == "consumed" else None)
                    html = render_card_widget(card, **self.target)
                    parsed = ParsedWidget(html)
                    buttons = [attrs for tag, attrs in parsed.elements if tag == "button"]
                    self.assertEqual(len(buttons), 1)
                    self.assertIn("disabled", buttons[0])
                    self.assertNotIn("data-hermes-send", html)
                    self.assertNotIn(card.card_id, html)
                    self.assertNotIn("request:private", html)
                    explanation_id = buttons[0]["aria-describedby"]
                    self.assertTrue(any(attrs.get("id") == explanation_id for _, attrs in parsed.elements))
                    self.assertIn(status, " ".join(parsed.text).lower())

    def test_exact_target_strings_are_text_not_script_or_event_attributes(self) -> None:
        target = {
            "app": '<script>alert("app")</script>',
            "runtime": '<script>alert("runtime")</script>',
            "status_text": '<script>alert("status")</script>',
            "project": '\"><img src="https://example.invalid" onerror="alert(1)">',
            "scheme": "  A & B < C 'quoted' \"scheme\"  ",
            "device": '<button data-hermes-send="arbitrary command" onclick="bad()">device</button>',
        }
        parsed = ParsedWidget(render_card_widget(self.card, **target))
        for value in target.values():
            self.assertIn(value, parsed.text)
        self.assertEqual(sum(tag == "button" for tag, _ in parsed.elements), 1)
        self.assertFalse(any(tag in ("script", "img", "iframe", "a", "form", "link")
                             for tag, _ in parsed.elements))
        for _, attrs in parsed.elements:
            self.assertFalse(any(name.startswith("on") for name in attrs))
            self.assertFalse(any(name in ("src", "href", "action") for name in attrs))
        self.assertEqual([attrs["data-hermes-send"] for _, attrs in parsed.elements
                          if "data-hermes-send" in attrs], [card_prompt(self.card)])

    def test_standalone_semantic_theme_and_focus_styles(self) -> None:
        html = render_card_widget(self.card, **self.target)
        parsed = ParsedWidget(html)
        self.assertTrue(html.lower().startswith("<!doctype html>"))
        self.assertIn(("html", {"lang": "en"}), parsed.elements)
        for tag in ("title", "main", "h1", "dl", "dt", "dd", "button"):
            self.assertTrue(any(element == tag for element, _ in parsed.elements), tag)
        for variable in ("foreground", "muted-foreground", "accent", "border", "card"):
            self.assertIn(f"var(--{variable})", html)
        self.assertIn(":focus-visible", html)
        self.assertIn("outline:", html)
        self.assertNotRegex(html, r"(?s)body\s*\{")
        self.assertNotIn("font-family", html)
        self.assertNotIn("<script", html)

    def test_preview_is_compact_with_closed_details_and_truthful_status(self) -> None:
        card = replace(self.card, action="preview")
        html = render_card_widget(card, **self.target, runtime="iOS 27", result_text="Visible pixels: unknown.")
        parsed = ParsedWidget(html)
        details = [attrs for tag, attrs in parsed.elements if tag == "details"]
        self.assertEqual(len(details), 1)
        self.assertNotIn("open", details[0])
        self.assertIn("Ready to open", parsed.text)
        self.assertIn("iOS 27", parsed.text)
        self.assertIn("Open app", parsed.text)
        self.assertIn("overflow-wrap: anywhere", html)
        self.assertIn("summary:focus-visible", html)
        for text in ("Running", "Stopped", "Last open succeeded", "verified preview"):
            self.assertNotIn(text, html)

    def test_existing_record_validation_rejects_unknown_action_or_status(self) -> None:
        for changes in ({"action": "arbitrary command"}, {"status": "executing"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.card, **changes)

    def test_import_and_render_are_pure_and_preserve_all_statuses(self) -> None:
        effects = [
            "subprocess.run", "subprocess.Popen", "os.system", "socket.socket",
            "urllib.request.urlopen", "pathlib.Path.write_text", "pathlib.Path.write_bytes",
            "builtins.open", "os.open",
        ]
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in ("subprocess", "socket", "urllib") or "native" in name:
                raise AssertionError(f"forbidden import: {name}")
            return original_import(name, *args, **kwargs)

        path = Path(__file__).resolve().parents[2] / "plugin/card_widget.py"
        spec = importlib.util.spec_from_file_location("plugin._isolated_card_widget_test", path)
        module = importlib.util.module_from_spec(spec)
        with ExitStack() as stack:
            traps = [stack.enter_context(patch(name, side_effect=AssertionError(name))) for name in effects]
            stack.enter_context(patch("builtins.__import__", side_effect=guarded_import))
            stack.enter_context(patch.dict(sys.modules, {spec.name: module}))
            spec.loader.exec_module(module)
            for status in ("pending", "consumed", "retired"):
                card = replace(self.card, status=status,
                               consumed_request_id="request:private" if status == "consumed" else None)
                before = asdict(card)
                first = module.render_card_widget(card, **self.target)
                self.assertEqual(first, module.render_card_widget(card, **self.target))
                self.assertEqual(asdict(card), before)
            for trap in traps:
                trap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
