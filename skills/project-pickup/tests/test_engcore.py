"""Unit tests for the read-only guarantees in engcore (no network, no real repos)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path

import helpers  # noqa: F401  (sets sys.path)
import engcore
from engcore import Budget, GitHub, SourceError, UnsafeOperation


def responder(*responses):
    """A transport that returns the given (status, headers, body) tuples in order."""
    calls = []

    def transport(request, timeout):
        calls.append(request)
        result = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(result, Exception):
            raise result
        status, headers, payload = result
        return status, headers, json.dumps(payload).encode() if not isinstance(payload, bytes) else payload

    transport.calls = calls
    return transport


def client(transport, max_requests=10, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    return GitHub("tok_SECRET_123", Budget(max_requests=max_requests, deadline_s=60), transport=transport, sleep=sleeps.append)


class GraphQLGuardTest(unittest.TestCase):
    def test_shipped_operations_are_queries_only(self):
        names = engcore.vet_all()
        self.assertIn("open_prs", names)
        self.assertIn("project_items_user", names)

    def test_operation_types(self):
        cases = {
            "query Q { viewer { login } }": ["query"],
            "{ viewer { login } }": ["query"],
            "mutation M { addStar(input: {}) { clientMutationId } }": ["mutation"],
            "subscription S { x }": ["subscription"],
            "query A { a } mutation B { b }": ["query", "mutation"],
            'query Q { search(query: "mutation subscription") { issueCount } }': ["query"],
            "# mutation in a comment\nquery Q { a }": ["query"],
            "query Q($a: String = \"}mutation{\") { a }": ["query"],
            "fragment F on User { login } query Q { viewer { ...F } }": ["fragment", "query"],
        }
        for document, expected in cases.items():
            with self.subTest(document=document):
                self.assertEqual(engcore.operation_types(document), expected)

    def test_mutation_file_is_refused_at_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "evil.graphql").write_text("query Ok { a }\nmutation Bad { deleteIssue(input: {}) { clientMutationId } }")
            with self.assertRaises(UnsafeOperation):
                engcore.load_graphql("evil", Path(tmp))
            with self.assertRaises(UnsafeOperation):
                engcore.vet_all(Path(tmp))

    def test_operation_name_cannot_escape_ops_dir(self):
        with self.assertRaises(UnsafeOperation):
            engcore.load_graphql("../collect")

    def test_unvetted_document_is_refused(self):
        fake = engcore.VettedDocument(name="open_prs", text="mutation X { y }")
        transport = responder((200, {}, {"data": {}}))
        with self.assertRaises(UnsafeOperation):
            client(transport).graphql(fake, {})
        self.assertEqual(transport.calls, [])


class HttpGuardTest(unittest.TestCase):
    def test_only_get_and_graphql_post(self):
        transport = responder((200, {}, {}))
        gh = client(transport)
        for method, path in (("PUT", "/repos/a/b"), ("DELETE", "/repos/a/b"), ("PATCH", "/repos/a/b"), ("POST", "/repos/a/b/issues")):
            with self.subTest(method=method), self.assertRaises(UnsafeOperation):
                gh._request(method, path, body=b"{}")
        for path in ("/graphql", "repos/a/b", "/repos/../x", "//evil.example/x"):
            with self.subTest(path=path), self.assertRaises(UnsafeOperation):
                gh.rest_get(path)
        self.assertEqual(transport.calls, [])

    def test_get_sends_bearer_token_and_no_body(self):
        transport = responder((200, {}, {"ok": True}))
        self.assertEqual(client(transport).rest_get("/repos/a/b", {"per_page": 1}), {"ok": True})
        request = transport.calls[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("Authorization"), "Bearer tok_SECRET_123")
        self.assertTrue(request.full_url.startswith("https://api.github.com/repos/a/b?"))

    def test_retries_5xx_then_succeeds(self):
        sleeps = []
        transport = responder((502, {}, b"bad gateway"), (200, {}, {"ok": 1}))
        self.assertEqual(client(transport, sleeps=sleeps).rest_get("/x"), {"ok": 1})
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(sleeps), 1)

    def test_4xx_is_not_retried(self):
        transport = responder((404, {}, {"message": "Not Found"}))
        with self.assertRaisesRegex(SourceError, "HTTP 404"):
            client(transport).rest_get("/x")
        self.assertEqual(len(transport.calls), 1)

    def test_secondary_rate_limit_honours_retry_after(self):
        sleeps = []
        transport = responder((403, {"retry-after": "3"}, {"message": "secondary rate limit"}), (200, {}, {}))
        client(transport, sleeps=sleeps).rest_get("/x")
        self.assertEqual(sleeps, [3])

    def test_retries_are_capped(self):
        transport = responder((500, {}, b"x"))
        with self.assertRaises(SourceError):
            client(transport).rest_get("/x")
        self.assertEqual(len(transport.calls), 1 + engcore.MAX_RETRIES)

    def test_request_budget(self):
        transport = responder((200, {}, {}))
        gh = client(transport, max_requests=2)
        gh.rest_get("/a")
        gh.rest_get("/b")
        with self.assertRaises(engcore.BudgetExceeded):
            gh.rest_get("/c")

    def test_token_never_in_errors(self):
        transport = responder(urllib.error.URLError("connect to tok_SECRET_123 failed"))
        with self.assertRaises(SourceError) as caught:
            client(transport).rest_get("/x")
        self.assertNotIn("tok_SECRET_123", str(caught.exception))

    def test_graphql_errors_become_source_errors(self):
        doc = engcore.load_graphql("open_prs")
        transport = responder((200, {}, {"data": {"repository": None}, "errors": [{"type": "NOT_FOUND", "message": "nope"}]}))
        with self.assertRaisesRegex(SourceError, "not found or no access"):
            client(transport).graphql(doc, {})

    def test_low_rate_limit_stops_graphql(self):
        doc = engcore.load_graphql("open_prs")
        transport = responder((200, {}, {"data": {"rateLimit": {"remaining": 100}}}))
        gh = client(transport)
        gh.graphql(doc, {})
        with self.assertRaises(engcore.BudgetExceeded):
            gh.graphql(doc, {})


class TokenTest(unittest.TestCase):
    def test_token_file_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "t")
            with self.assertRaisesRegex(SourceError, "not found"):
                engcore.read_token(path)
            path.write_text("abc\n")
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(SourceError, "chmod 600"):
                engcore.read_token(path)
            os.chmod(path, 0o600)
            self.assertEqual(engcore.read_token(path), "abc")
            path.write_text("a b")
            with self.assertRaisesRegex(SourceError, "malformed"):
                engcore.read_token(path)


class GitGuardTest(unittest.TestCase):
    def test_write_subcommands_are_refused(self):
        for args in (["commit", "-m", "x"], ["fetch"], ["stash"], ["worktree", "remove", "x"], ["worktree", "lock", "x"],
                     ["remote", "add", "x", "y"], ["reset", "--hard"], ["checkout", "main"]):
            with self.subTest(args=args), self.assertRaises(UnsafeOperation):
                engcore.git(args, Path("."))

    def test_parse_github_remote(self):
        self.assertEqual(engcore.parse_github_remote("git@github.com:acme/widgets.git"), "acme/widgets")
        self.assertEqual(engcore.parse_github_remote("https://github.com/acme/widgets"), "acme/widgets")
        self.assertEqual(engcore.parse_github_remote("ssh://git@github.com/acme/w.x.git"), "acme/w.x")
        self.assertIsNone(engcore.parse_github_remote("git@gitlab.com:acme/widgets.git"))
        self.assertIsNone(engcore.parse_github_remote("https://github.com/acme/widgets/../../x"))


class ValidateTest(unittest.TestCase):
    def test_subset(self):
        schema = {"type": "object", "required": ["a"], "additionalProperties": False,
                  "properties": {"a": {"type": "integer", "minimum": 1}, "b": {"enum": ["x", None]}}}
        self.assertEqual(engcore.validate({"a": 1, "b": None}, schema), [])
        self.assertTrue(engcore.validate({"a": 0}, schema))
        self.assertTrue(engcore.validate({"a": True}, schema))
        self.assertTrue(engcore.validate({"a": 1, "c": 2}, schema))
        self.assertTrue(engcore.validate({}, schema))


class WordingTest(unittest.TestCase):
    def test_banned_words(self):
        self.assertEqual(engcore.banned_words_in("This PR is overdue and Stale"), ["overdue", "stale"])
        self.assertEqual(engcore.banned_words_in("3 commits behind main; translate later"), [])


if __name__ == "__main__":
    unittest.main()
