"""Golden corpus + property tests for the redactors (DESIGN.md §7)."""

from __future__ import annotations

import random
import string

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from personal_os.privacy.redact import REDACTED, TextRedactor, looks_random, shannon_entropy

R = TextRedactor()


def redact(text: str, ctx="text") -> str:
    return R.redact(text, ctx)[0]


# (input, the secret that must disappear, context)
PLANTED = [
    ("aws AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE", "text"),
    (
        "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "wJalrXUtnFEMI",
        "command",
    ),
    # Fake keys are assembled from pieces so no literal in the repo looks like a real secret
    # to secret scanners (GitHub push protection); the assembled strings have the real shape.
    ("token " + "gh" + "p_" + "A1b2C3d4" * 5, "p_A1b2", "text"),
    ("github" + "_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz", "_pat_11", "text"),
    ("gl" + "pat-abcdefghij0123456789xyz", "pat-abc", "text"),
    ("slack " + "xo" + "xb-123456789012-abcdefghijkl", "xb-1234", "text"),
    ("stripe " + "sk" + "_live_51HabcdefghijklmnopQRST", "_live_51", "text"),
    ("OPENAI_API_KEY=" + "sk" + "-proj-Abcdefghijklmnopqrstuvwxyz0123", "-proj-Abc", "command"),
    ("anthropic " + "sk" + "-ant-api03-abcdefghijklmnopqrstuvwxyz", "-ant-api03", "text"),
    ("maps " + "AI" + "zaSyD-abcdefghijklmnopqrstuvwxyz0123456", "zaSyD", "text"),
    (
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "eyJhbGciOiJIUzI1NiJ9",
        "text",
    ),
    (
        'curl -H "Authorization: Bearer abc123def456ghi789" https://x',
        "abc123def456ghi789",
        "command",
    ),
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA", "text"),
    ("proxy-authorization: token s3cr3tvalue", "s3cr3tvalue", "text"),
    ("git clone https://alice:Pa55word@github.com/a/b.git", "Pa55word", "git"),
    ("postgres://app:SuperS3cret@db.local:5432/app", "SuperS3cret", "text"),
    ("app --password hunter2 --verbose", "hunter2", "command"),
    ("app --password=hunter2", "hunter2", "command"),
    ("app --api-key 'quoted key value'", "quoted key value", "command"),
    ("app -token abcd1234", "abcd1234", "command"),
    ("mysql -u root -phunter2 db", "hunter2", "command"),
    ("mysqldump --user=root -pTopSecret1 db > out.sql", "TopSecret1", "command"),
    ('$env:GITHUB_TOKEN = "plainvalue"', "plainvalue", "command"),
    ("set DB_PASSWORD=letmein", "letmein", "command"),
    ("client_secret: abcdefgh", "abcdefgh", "text"),
    ("Server=db;Database=app;User Id=sa;Password=Yukon900;", "Yukon900", "text"),
    (
        "-----BEGIN RSA "
        + "PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA "
        + "PRIVATE KEY-----",
        "MIIEowIBAAKCAQEA",
        "text",
    ),
    (
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA truncated",
        "b3BlbnNzaC1rZXkt",
        "text",
    ),
    ("random kX9vQ2mL7pR4tY8wZ1nB5cJ3 token", "kX9vQ2mL7pR4tY8wZ1nB5cJ3", "text"),
    (
        "hex 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "9f86d081884c7d65",
        "text",
    ),
    ("open https://example.com/reset?token=abcdef&utm_source=x", "abcdef", "text"),
    ("https://example.com/cb?code=4/0AbCdEf&state=xyz", "4/0AbCdEf", "url"),
    ("https://example.com/#access_token=ya29.secret&expires=3600", "ya29.secret", "url"),
]

BENIGN = [
    ("git checkout 3f2a9c1e5b7d4a6f8e0c2b4d6f8a0c2e4b6d8f0a", "command"),
    ("Merge commit 3f2a9c1e5b7d4a6f8e0c2b4d6f8a0c2e4b6d8f0a into main", "git"),
    ("https://github.com/a/b/commit/3f2a9c1e5b7d4a6f8e0c2b4d6f8a0c2e4b6d8f0a", "url"),
    ("id 550e8400-e29b-41d4-a716-446655440000", "text"),
    ("cd C:/Users/addya/Documents/my_long_project_name_2024", "command"),
    ("python -m pytest tests/test_session.py -q", "command"),
    ("npm install @types/node typescript_eslint_parser", "command"),
    ("session.py - auth-service - Visual Studio Code", "title"),
    ("How to retry httpx requests when the OAuth refresh token expires - Stack Overflow", "title"),
    ("SomeVeryLongCamelCaseIdentifierName", "text"),
    ("uv run personal-os sim run workday.toml", "command"),
    ("Get-ChildItem -Recurse -Filter *.py", "command"),
    ("short abc123", "text"),
    ("docs.python.org/3.12/library/sqlite3.html", "text"),
    ("tokenizer.py - nlp - Visual Studio Code", "title"),
    ("refresh_token() returns a new access token", "text"),
]


@pytest.mark.parametrize(("text", "secret", "ctx"), PLANTED)
def test_planted_secret_is_removed(text: str, secret: str, ctx):
    out = redact(text, ctx) if ctx != "url" else R.redact_url(text)
    assert secret not in out, out


@pytest.mark.parametrize(("text", "ctx"), BENIGN)
def test_benign_text_is_untouched(text: str, ctx):
    out = R.redact_url(text) if ctx == "url" else redact(text, ctx)
    assert out == text


def test_benign_false_positive_rate_under_two_percent():
    rng = random.Random(7)
    words = ["session", "auth", "token", "refresh", "build", "test", "main", "src", "docs", "api"]
    samples = []
    for _ in range(500):
        n = rng.randint(3, 10)
        samples.append(" ".join(rng.choice(words) + rng.choice(["", "_v2", ".py", "-2024", "s"])
                                for _ in range(n)))  # fmt: skip
    changed = sum(redact(s) != s for s in samples)
    assert changed / len(samples) < 0.02


def test_entropy_recall_on_random_tokens():
    rng = random.Random(42)
    alphabet = string.ascii_letters + string.digits + "_-"
    tokens = ["".join(rng.choice(alphabet) for _ in range(rng.randint(24, 64))) for _ in range(500)]
    caught = sum(looks_random(t, "text") for t in tokens)
    assert caught / len(tokens) >= 0.97


def test_url_redaction_strips_tracking_and_userinfo_keeps_meaning():
    url = "https://User:Pw@Docs.Example.com:443/a/b?q=sqlite+wal&utm_campaign=x&fbclid=1#section-2"
    assert R.redact_url(url) == "https://docs.example.com/a/b?q=sqlite+wal#section-2"


def test_redaction_is_idempotent():
    text = "curl -H 'Authorization: Bearer abc123def456ghi789' --password hunter2"
    once = redact(text, "command")
    assert redact(once, "command") == once
    assert once.count(REDACTED) == 2


def test_hits_are_counted_by_detector():
    _, hits = R.redact("--password hunter2 AKIAIOSFODNN7EXAMPLE", "command")
    assert hits["secret_flag"] == 1 and hits["aws_access_key"] == 1


def test_entropy_of_uniform_string():
    assert shannon_entropy("abcd") == pytest.approx(2.0)


_SECRETS = st.sampled_from(
    [
        "AKIAIOSFODNN7EXAMPLE",
        "gh" + "p_" + "Z9y8X7w6" * 5,
        "xo" + "xb-123456789012-abcdefghijkl",
        "sk" + "-ant-api03-abcdefghijklmnopqrstuvwxyz",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
    ]
)
_SURROUND = st.text(alphabet=string.ascii_letters + string.digits + " .,:/()-", max_size=60)


@settings(max_examples=300, deadline=None)
@given(prefix=_SURROUND, secret=_SECRETS, suffix=_SURROUND)
def test_property_known_secret_never_survives(prefix: str, secret: str, suffix: str):
    for ctx in ("text", "command", "title"):
        assert secret not in redact(f"{prefix} {secret} {suffix}", ctx)


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=300))
def test_property_redactor_never_raises(text: str):
    R.redact(text, "text")
    R.redact_url(text)
