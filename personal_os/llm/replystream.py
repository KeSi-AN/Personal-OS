"""The spoken reply, sentence by sentence, while the model is still writing its JSON.

The agent's step is JSON (`{"decide": ..., "calls": [...], "reply": "...", "details": ...}`).
Its grammar fixes the key order, so `decide` is known before `reply` starts. This reads the
streamed text a character at a time, follows the JSON just enough to find the top-level
`reply` string, decodes its escapes, and hands on each finished sentence, so voice can say the
first one while the rest is being written (as Roamies does with its own reply stream).

Only a reply that is the answer is handed on: when `decide` is "use_tools", the reply belongs
to a step whose tools haven't run yet, so nothing is said from it. A list (a line break in the
reply) stops it: how a list is said is decided once it's complete (voice.main.spoken).
Pure and synchronous, so it's tested without a model.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# A sentence ends at . ! or ? (and any closing quotes or brackets) followed by a space.
_END = re.compile(r"[.!?][\"')\]]*\s")
_MIN_SENTENCE = 12  # "e.g. " or "Dr. " alone isn't a sentence worth sending to the voice


class ReplyStream:
    """Feed it the raw text as it arrives; `on_sentence` gets each finished sentence of the
    top-level "reply". `gate`: the "decide" value that allows speaking (None: no gate, as for
    a reply-only answer)."""

    def __init__(self, on_sentence: Callable[[str], None], *, gate: str | None = "answer"):
        self.on_sentence = on_sentence
        self.gate = gate
        self.depth = 0
        self.in_string = False
        self.escape = ""  # "" or the escape read so far ("\\", "\\u00")
        self.text = ""  # the string being read
        self.key = ""  # the last top-level key read
        self.expect_value = False  # a top-level key and ":" were read
        self.capturing = ""  # "reply" or "decide" while reading that value
        self.decide = ""
        self.pending = ""  # reply text not yet sent as a sentence
        self.sent: list[str] = []
        self.stopped = False  # a list began, or the reply ended: nothing more is sent

    # --- reading JSON ------------------------------------------------------------------------

    def feed(self, chunk: str) -> None:
        for ch in chunk:
            self._char(ch)

    def _char(self, ch: str) -> None:
        if self.in_string:
            self._string_char(ch)
            return
        if ch == '"':
            self.in_string, self.text = True, ""
            if self.depth == 1 and self.expect_value and self.key in ("reply", "decide"):
                self.capturing = self.key
            return
        if ch in "{[":
            self.depth += 1
            self.expect_value = False
        elif ch in "}]":
            self.depth -= 1
        elif ch == ":" and self.depth == 1:
            self.expect_value = True
        elif ch == "," and self.depth == 1:
            self.expect_value = False

    def _string_char(self, ch: str) -> None:
        if self.escape:
            self.escape += ch
            if self.escape.startswith("\\u"):
                if len(self.escape) < 6:
                    return
                try:
                    decoded = chr(int(self.escape[2:], 16))
                except ValueError:
                    decoded = ""
            else:
                decoded = {"n": "\n", "t": " ", "r": "", "b": "", "f": ""}.get(ch, ch)
            self.escape = ""
            self._string_text(decoded)
            return
        if ch == "\\":
            self.escape = "\\"
            return
        if ch == '"':
            self._string_end()
            return
        self._string_text(ch)

    def _string_text(self, s: str) -> None:
        self.text += s
        if self.capturing == "reply" and self._speaking():
            self.pending += s
            self._send_ready()

    def _string_end(self) -> None:
        self.in_string = False
        if self.capturing == "decide":
            self.decide = self.text
        elif self.capturing == "reply":
            self.finish()
        elif self.depth == 1 and not self.expect_value:
            self.key = self.text  # a top-level key; its value follows the ":"
        self.capturing = ""
        if self.depth == 1 and self.expect_value:
            self.expect_value = False

    # --- sentences ---------------------------------------------------------------------------

    def _speaking(self) -> bool:
        return not self.stopped and (self.gate is None or self.decide == self.gate)

    def _send_ready(self) -> None:
        if "\n" in self.pending:
            self._send(self.pending.split("\n", 1)[0])
            self.pending = ""
            self.stopped = True  # a list: said once it's complete
            return
        start = 0
        for m in _END.finditer(self.pending):
            if m.end() - start >= _MIN_SENTENCE:
                self._send(self.pending[start : m.end()])
                start = m.end()
        self.pending = self.pending[start:]

    def _send(self, sentence: str) -> None:
        sentence = " ".join(sentence.split())
        if sentence and not self.stopped:
            self.sent.append(sentence)
            self.on_sentence(sentence)

    def finish(self) -> None:
        """The reply is complete (or the stream ended): send what's left."""
        if self._speaking() and self.pending.strip():
            self._send(self.pending)
        self.pending = ""
        self.stopped = True

    @property
    def said(self) -> str:
        return " ".join(self.sent)
