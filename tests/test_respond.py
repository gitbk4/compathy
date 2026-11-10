"""Tests for respond.py: the /compathy-respond bookkeeping and guardrails."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import compathy_query as cq  # noqa: E402
import lint  # noqa: E402
import persona_integration  # noqa: E402
import respond  # noqa: E402
import scaffold  # noqa: E402
from paths import respond_dir, respond_ledger_path, respond_policy_path, respond_sources_path, schema_path, wiki_dir  # noqa: E402

GDOC = "https://docs.google.com/document/d/abc123/edit?usp=sharing"
CLAUDE = "https://claude.ai/artifact/XYZ789"


def _page(type_, slug, body="", sources="[]", title=None):
    title = title or slug.replace("-", " ").title()
    return (
        f"---\ntype: {type_}\nschema_version: 1\ncreated: 2026-04-04\n"
        f"updated: 2026-04-04\nsources: {sources}\n---\n# {title}\n\n{body}\n"
    )


class RespondFixture(unittest.TestCase):
    """A small wiki plus an initialised context/respond/."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        base = Path(self.td.name)
        self._old_home = os.environ.get("HOME")
        self._old_persona_path = persona_integration.PERSONA_JSON_PATH
        fake_home = base / "_fake_home"
        fake_home.mkdir()
        os.environ["HOME"] = str(fake_home)
        persona_integration.PERSONA_JSON_PATH = fake_home / ".ai-quickstart" / "persona" / "persona.json"
        scaffold.create_structure(base / "proj", project_name="demo")
        self.root = base / "proj"
        self.wiki = wiki_dir(self.root)
        (self.wiki / "concepts" / "alpha.md").write_text(_page(
            "concept", "alpha",
            f"Alpha explains the roadmap. See {GDOC} and the page {CLAUDE}. hello alpha",
            sources="[spec.md]"))
        (self.wiki / "concepts" / "roadmap.md").write_text(_page(
            "concept", "roadmap", "Where we are and what moves the dates.", sources="[spec.md]"))
        (self.wiki / "entities" / "jane-doe.md").write_text(_page(
            "entity", "jane-doe", "Jane is the DRI.", sources="[spec.md]", title="Jane Doe"))
        (self.wiki / "summaries" / "slack-threads.md").write_text(_page(
            "summary", "slack-threads", "hello from a thread", sources="[slack-threads-2026.md]"))
        (self.wiki / "summaries" / "spec.md").write_text(_page(
            "summary", "spec", "The spec summary.", sources="[spec.md]"))
        (self.wiki / "patterns" / "technical-patterns.md").write_text(_page(
            "patterns", "technical-patterns", "Stdlib only."))
        (self.wiki / "index.md").write_text(
            "---\ntype: index\nschema_version: 1\n---\n# Index\n\n## Concepts\n"
            "- [[alpha]]: the alpha concept\n- [[roadmap]]: the roadmap\n\n## Entities\n- [[jane-doe]]: the DRI\n\n"
            "## Summaries\n- [[slack-threads]]: threads\n- [[spec]]: the spec\n\n## Patterns\n- [[technical-patterns]]: how code is written\n")
        cq.EXTRA_LAYERS = []
        self.init_result = self.run_cmd(
            "init", "--project-name", "demo", "--owner-name", "Bill",
            "--owner-slack-id", "U0OWNER", "--internal-domain", "example.com",
            "--roadmap-page", "roadmap")

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        persona_integration.PERSONA_JSON_PATH = self._old_persona_path
        self.td.cleanup()

    # helpers
    def run_cmd(self, *argv):
        args = respond.build_parser().parse_args(["--target", str(self.root), *argv])
        return respond.COMMANDS[args.cmd](self.root, args)

    def set_policy(self, key, value):
        p = respond_policy_path(self.root)
        text = p.read_text()
        new, n = re.subn(rf"^{re.escape(key)}:.*$", f"{key}: {value}", text, count=1, flags=re.M)
        self.assertEqual(n, 1, f"policy key {key} not found")
        p.write_text(new)

    def mark_shared(self, url_fragment, verified=None):
        if verified is None:
            verified = datetime.now().date().isoformat()
        p = respond_sources_path(self.root)
        lines = []
        for line in p.read_text().splitlines():
            if url_fragment in line and line.startswith("|"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                cells[0] = "Doc " + url_fragment[:6]
                cells[4] = "shared"
                cells[5] = verified
                line = "| " + " | ".join(cells) + " |"
            lines.append(line)
        p.write_text("\n".join(lines) + "\n")

    def open_window(self, hours=2, mode="live"):
        until = (datetime.now().astimezone() + timedelta(hours=hours)).isoformat(timespec="seconds")
        return self.run_cmd("window", "start", "--until", until, "--mode", mode)

    def good_draft(self, key="C1:100.1", **over):
        policy, _ = respond.load_policy(self.root)
        d = {
            "outcome": "answered", "intent": "feature", "confidence": "high",
            "thread_key": key, "asker": "U0ASKER", "asker_email": "a@example.com",
            "message": f"{policy['header']}\nBill is back Monday.\n\nAlpha explains the roadmap.\n\n"
                       f"*Sources:* Doc ({GDOC})\n{policy['footer']}",
            "pages": ["alpha"], "links": [GDOC], "house_answer": False,
            "channel": "C1 (#test)", "question": "what is alpha", "reply_ts": "100.2", "mode": "live",
        }
        d.update(over)
        return d


class TestInitAndCorpus(RespondFixture):
    def test_init_scaffolds_and_seeds(self):
        rd = respond_dir(self.root)
        for name in ("policy.md", "sources.md", "lessons.md", "ledger.md", ".respond-state.json"):
            self.assertTrue((rd / name).is_file(), name)
        self.assertEqual(self.init_result["deny_slugs_seeded"], ["jane-doe"])
        self.assertEqual(self.init_result["registry_seeded"], 2)
        # a fresh scaffold's schema.md already carries the section, so init adds nothing
        self.assertFalse(self.init_result["schema_section_added"])
        self.assertIn("## Respond", schema_path(self.root).read_text())
        excluded = {e["slug"] for e in self.init_result["excluded_by_filters"]}
        self.assertEqual(excluded, {"slack-threads"})
        policy, _ = respond.load_policy(self.root)
        self.assertEqual(policy["owner_slack_id"], "U0OWNER")
        self.assertEqual(policy["deny_slugs"], ["jane-doe"])
        self.assertIn("Bill's stand-in", policy["header"])

    def test_init_refuses_second_run_without_force(self):
        with self.assertRaises(respond.Refusal):
            self.run_cmd("init")

    def test_init_force_appends_schema_section_once_and_keeps_ledger(self):
        sp = schema_path(self.root)
        text = sp.read_text()
        sp.write_text(text[: text.index("## Respond")].rstrip("\n") + "\n")
        ledger = respond_ledger_path(self.root)
        ledger.write_text(ledger.read_text() + "\n## [2026-10-29T10:00:00-07:00] R-0001 | answered | feature | high\n\n- thread: C1:1.1\n")
        out = self.run_cmd("init", "--force", "--owner-name", "Bill")
        self.assertTrue(out["schema_section_added"])
        self.assertEqual(sp.read_text().count("## Respond"), 1)
        self.assertIn("R-0001", ledger.read_text())
        self.assertFalse(self.run_cmd("init", "--force")["schema_section_added"])

    def test_corpus_filters(self):
        out = self.run_cmd("corpus")
        allowed = {p["slug"] for p in out["pages"]}
        self.assertEqual(allowed, {"alpha", "roadmap", "spec", "technical-patterns"})
        reasons = {e["slug"]: e["reasons"] for e in out["excluded"]}
        self.assertIn("listed in deny_slugs", reasons["jane-doe"])
        self.assertTrue(any("matches slack" in r for r in reasons["slack-threads"]))
        self.assertEqual(next(p for p in out["pages"] if p["slug"] == "alpha")["summary"], "the alpha concept")

    def test_allow_slugs_overrides_source_patterns_not_deny(self):
        self.set_policy("allow_slugs", "[slack-threads, jane-doe]")
        allowed = {p["slug"] for p in self.run_cmd("corpus")["pages"]}
        self.assertIn("slack-threads", allowed)
        self.assertNotIn("jane-doe", allowed)

    def test_search_stays_inside_corpus(self):
        out = self.run_cmd("search", "--query", "hello")
        slugs = [r["slug"] for r in out["results"]]
        self.assertIn("alpha", slugs)
        self.assertNotIn("slack-threads", slugs)

    def test_short_queries_match_whole_words(self):
        (self.wiki / "concepts" / "method.md").write_text(_page("concept", "method", "The evaluation method.", sources="[spec.md]"))
        idx = self.wiki / "index.md"
        idx.write_text(idx.read_text().replace("- [[roadmap]]: the roadmap", "- [[roadmap]]: the roadmap\n- [[method]]: the method"))
        slugs = [r["slug"] for r in self.run_cmd("search", "--query", "HOD")["results"]]
        self.assertEqual(slugs, [])
        (self.wiki / "concepts" / "alpha.md").write_text(_page("concept", "alpha", "HOD means hospital outpatient department.", sources="[spec.md]"))
        slugs = [r["slug"] for r in self.run_cmd("search", "--query", "hod")["results"]]
        self.assertEqual(slugs, ["alpha"])

    def test_page_refuses_excluded_and_classifies_links(self):
        with self.assertRaises(respond.Refusal):
            self.run_cmd("page", "--slug", "slack-threads")
        with self.assertRaises(respond.Refusal):
            self.run_cmd("page", "--slug", "jane-doe")
        page = self.run_cmd("page", "--slug", "alpha")
        by_url = {l["url"]: l for l in page["links"]}
        self.assertEqual(by_url[GDOC]["registry"], "unknown")
        self.assertFalse(by_url[GDOC]["shareable"])
        self.mark_shared("abc123")
        page = self.run_cmd("page", "--slug", "alpha")
        by_url = {l["url"]: l for l in page["links"]}
        self.assertTrue(by_url[GDOC]["shareable"])
        self.assertFalse(by_url[CLAUDE]["shareable"])

    def test_sources_match_and_status(self):
        self.mark_shared("abc123")
        out = self.run_cmd("sources", "--status", "shared")
        self.assertEqual(out["count"], 1)
        self.assertTrue(out["entries"][0]["shareable"])
        self.assertEqual(self.run_cmd("sources", "--match", "XYZ789")["count"], 1)

    def test_kind_inference_never_auto_shares_unknown_hosts(self):
        self.assertEqual(respond._kind_for("https://midstreamhealth-my.sharepoint.com/:x:/g/x"), "sharepoint")
        self.assertEqual(respond._kind_for("https://www.hrsa.gov/opa"), "public")
        self.assertEqual(respond._kind_for("https://example.com/doc"), "other")
        self.run_cmd("sources", "set", "--url", "https://example.com/doc", "--status", "unknown", "--via", "seed")
        self.run_cmd("sources", "apply-rules")
        rows = {e["url"]: e for e in self.run_cmd("sources")["entries"]}
        self.assertEqual(rows["https://example.com/doc"]["status"], "unknown")
        self.run_cmd("sources", "set", "--url", "https://x.sharepoint.com/y", "--status", "shared", "--kind", "sharepoint", "--audience", "internal")
        rows = {e["url"]: e for e in self.run_cmd("sources")["entries"]}
        self.assertEqual((rows["https://x.sharepoint.com/y"]["kind"], rows["https://x.sharepoint.com/y"]["via"]), ("sharepoint", "manual"))

    def test_normalize_url(self):
        a = respond.normalize_url("https://docs.google.com/document/d/x/edit?usp=sharing&authuser=me#h")
        b = respond.normalize_url("https://docs.google.com/document/d/x/edit")
        self.assertEqual(a, b)
        self.assertEqual(respond.normalize_url("https://a.b/c/)."), "https://a.b/c")


class TestRegistryVerification(RespondFixture):
    LINEAR = "https://linear.app/acme/project/thing-123"
    NOTION = "https://app.notion.com/p/3d551886329181d88e5acdbc31eb4a44"

    def test_seed_adds_new_corpus_links_once(self):
        (self.wiki / "concepts" / "roadmap.md").write_text(_page(
            "concept", "roadmap", "See https://www.cms.gov/rule and https://linear.app/acme/issue/X-1.", sources="[spec.md]"))
        out = self.run_cmd("sources", "seed")
        self.assertEqual(sorted(out["added"]), ["https://linear.app/acme/issue/X-1", "https://www.cms.gov/rule"])
        self.assertEqual(self.run_cmd("sources", "seed")["count"], 0)
        self.run_cmd("sources", "apply-rules")
        rows = {e["url"]: e for e in self.run_cmd("sources")["entries"]}
        self.assertEqual((rows["https://www.cms.gov/rule"]["status"], rows["https://www.cms.gov/rule"]["audience"]), ("shared", ["all"]))
        self.assertEqual(rows["https://linear.app/acme/issue/X-1"]["via"], "rule:linear")

    def test_drive_verdict(self):
        v = respond.drive_verdict([{"type": "anyone", "role": "reader"}], "example.com")
        self.assertEqual((v["status"], v["audience"], v["via"]), ("shared", "all", "drive:anyone"))
        v = respond.drive_verdict([{"type": "domain", "emailAddress": "example.com", "role": "commenter"},
                                   {"type": "user", "emailAddress": "o@example.com", "role": "owner"}], "example.com")
        self.assertEqual((v["status"], v["audience"], v["via"]), ("shared", "internal", "drive:domain"))
        v = respond.drive_verdict([{"type": "domain", "emailAddress": "other.com", "role": "reader"},
                                   {"type": "user", "emailAddress": "a@example.com", "role": "reader"}], "example.com")
        self.assertEqual((v["status"], v["via"]), ("private", "drive:users"))
        v = respond.drive_verdict([{"type": "user", "emailAddress": "o@example.com", "role": "owner"}], "example.com")
        self.assertEqual((v["status"], v["via"]), ("private", "drive:owner-only"))

    def test_notion_verdict_and_ids(self):
        ids = respond.link_ids(self.NOTION)
        self.assertEqual(ids["notion_page_id"], "3d551886329181d88e5acdbc31eb4a44")
        self.assertEqual(respond.link_ids(GDOC)["drive_file_id"], "abc123")
        v = respond.notion_verdict(self.NOTION, ["3d551886-3291-81d8-8e5a-cdbc31eb4a44"], "shared")
        self.assertEqual((v["status"], v["via"]), ("private", "notion:private-section"))
        v = respond.notion_verdict(self.NOTION, ["ffff"], "shared")
        self.assertEqual((v["status"], v["via"]), ("shared", "notion:not-private"))
        v = respond.notion_verdict(self.NOTION, ["d965188632918212aa9e81a45ced07d8"], "shared",
                                   ancestor_ids=["d9651886-3291-8212-aa9e-81a45ced07d8"])
        self.assertEqual((v["status"], v["via"]), ("private", "notion:private-ancestor"))
        v = respond.notion_verdict(self.NOTION, ["ffff"], "shared", ancestor_ids=[])
        self.assertEqual(v["via"], "notion:not-private-checked")
        v = respond.notion_verdict(self.NOTION, ["ffff"], "shared", private_titles=["1:1 notes"],
                                   ancestor_titles=["Product", "1:1 Notes"])
        self.assertEqual(v["via"], "notion:private-ancestor")
        out = self.run_cmd("sources", "set", "--url", self.NOTION, "--status", "unknown", "--via", "seed")
        out = self.run_cmd("sources", "verdict", "--kind", "notion", "--url", self.NOTION, "--json",
                           json.dumps({"private_ids": [], "private_titles": ["Scratchpad"], "path": "Scratchpad / Ideas"}))
        self.assertEqual(out["results"][0]["via"], "notion:private-ancestor")
        self.assertEqual(respond.notion_verdict(self.NOTION, [], "unknown")["status"], "unknown")

    def test_apply_rules_and_manual_rows(self):
        self.run_cmd("sources", "set", "--url", self.LINEAR, "--status", "unknown", "--via", "seed")
        self.run_cmd("sources", "set", "--url", "https://www.fda.gov/x", "--status", "private", "--audience", "internal")
        out = self.run_cmd("sources", "apply-rules")
        self.assertIn(respond._clean_url(self.LINEAR), out["changed"])
        rows = {e["url"]: e for e in self.run_cmd("sources")["entries"]}
        self.assertEqual(rows[self.LINEAR]["status"], "shared")
        self.assertEqual(rows[self.LINEAR]["via"], "rule:linear")
        self.assertTrue(rows[self.LINEAR]["shareable"])
        self.assertEqual(rows["https://www.fda.gov/x"]["status"], "private")  # manual row untouched
        self.assertEqual(rows["https://www.fda.gov/x"]["via"], "manual")
        kept = self.run_cmd("sources", "set", "--url", "https://www.fda.gov/x", "--status", "shared", "--via", "rule:public")
        self.assertTrue(kept["kept"])
        forced = self.run_cmd("sources", "set", "--url", "https://www.fda.gov/x", "--status", "shared", "--via", "rule:public", "--force")
        self.assertFalse(forced["kept"])

    def test_stale_shared_is_not_shareable_and_listed(self):
        self.mark_shared("abc123", verified=(datetime.now() - timedelta(days=30)).date().isoformat())
        rows = {e["url"]: e for e in self.run_cmd("sources")["entries"]}
        self.assertTrue(rows[GDOC]["stale"])
        self.assertFalse(rows[GDOC]["shareable"])
        need = self.run_cmd("sources", "list", "--needs-verification")
        by_url = {e["url"]: e for e in need["entries"]}
        self.assertEqual(by_url[GDOC]["verifier"], "drive")
        self.assertEqual(by_url[GDOC]["drive_file_id"], "abc123")
        self.assertEqual(by_url[CLAUDE]["verifier"], "manual")
        self.open_window()
        policy, _ = respond.load_policy(self.root)
        state = respond.load_state(self.root)
        problems = respond.validate_draft(self.root, policy, state, self.good_draft())["problems"]
        self.assertTrue(any("stale" in p for p in problems))

    def test_verdict_cli_drive_and_notion(self):
        perms = {"permissions": [{"type": "domain", "emailAddress": "example.com", "role": "commenter"},
                                 {"type": "user", "emailAddress": "o@example.com", "role": "owner"}]}
        out = self.run_cmd("sources", "verdict", "--kind", "gdoc", "--url", GDOC, "--json", json.dumps(perms))
        self.assertFalse(out["applied"])
        out = self.run_cmd("sources", "verdict", "--kind", "gdoc", "--url", GDOC, "--json", json.dumps(perms), "--apply")
        self.assertEqual(out["results"][0]["via"], "drive:domain")
        rows = {e["url"]: e for e in self.run_cmd("sources")["entries"]}
        self.assertEqual(rows[GDOC]["status"], "shared")
        self.assertEqual(rows[GDOC]["verified"], datetime.now().date().isoformat())
        self.assertTrue(rows[GDOC]["shareable"])
        self.open_window()
        policy, _ = respond.load_policy(self.root)
        state = respond.load_state(self.root)
        self.assertEqual(respond.validate_draft(self.root, policy, state, self.good_draft())["problems"], [])
        self.run_cmd("sources", "set", "--url", self.NOTION, "--status", "unknown", "--via", "seed")
        out = self.run_cmd("sources", "verdict", "--kind", "notion", "--json",
                           json.dumps({"private_ids": ["3d551886329181d88e5acdbc31eb4a44"]}), "--apply")
        self.assertEqual(out["results"][0]["via"], "notion:private-section")
        rows = {e["url"]: e for e in self.run_cmd("sources")["entries"]}
        self.assertEqual(rows[self.NOTION]["status"], "private")
        # the table survives a round trip with every column
        text = respond_sources_path(self.root).read_text()
        self.assertIn("| Title | URL | Kind | Audience | Status | Verified | Via | Note |", text)
        self.assertEqual(text.count("| Title |"), 1)


class TestCheck(RespondFixture):
    def setUp(self):
        super().setUp()
        self.mark_shared("abc123")
        self.open_window()

    def problems(self, draft, **kw):
        policy, _ = respond.load_policy(self.root)
        state = respond.load_state(self.root)
        return respond.validate_draft(self.root, policy, state, draft, **kw)["problems"]

    def test_good_draft_passes(self):
        self.assertEqual(self.problems(self.good_draft()), [])

    def test_header_and_footer_required(self):
        d = self.good_draft(message="no header here")
        self.assertTrue(any("header" in p for p in self.problems(d)))
        policy, _ = respond.load_policy(self.root)
        d = self.good_draft(message=f"{policy['header']}\nanswer without footer", links=[])
        self.assertTrue(any("footer" in p for p in self.problems(d)))

    def test_unregistered_link_refused(self):
        d = self.good_draft()
        d["message"] = d["message"].replace(GDOC, CLAUDE)
        d["links"] = [CLAUDE]
        self.assertTrue(any("not in the registry" in p for p in self.problems(d)))

    def test_page_outside_corpus_refused(self):
        self.assertTrue(any("not in the corpus" in p for p in self.problems(self.good_draft(pages=["jane-doe"]))))

    def test_phrases_refused(self):
        d = self.good_draft()
        d["message"] += "\nI approve this."
        self.assertTrue(any("speaks as the owner" in p for p in self.problems(d)))
        self.set_policy("never_say", "[Acme Hospital]")
        d = self.good_draft()
        d["message"] += "\nAcme Hospital pays more."
        self.assertTrue(any("never_say" in p for p in self.problems(d)))

    def test_tiers_intents_and_who(self):
        self.assertTrue(any("confidence high" in p for p in self.problems(self.good_draft(confidence="medium"))))
        self.assertTrue(any("not supported" in p for p in self.problems(self.good_draft(intent="gossip"))))
        self.assertTrue(any("owner" in p for p in self.problems(self.good_draft(asker="U0OWNER"))))
        self.assertTrue(any("not internal" in p for p in self.problems(self.good_draft(asker_email="x@other.com"))))
        self.assertTrue(any("externally shared" in p for p in self.problems(self.good_draft(channel_external=True))))
        self.assertTrue(any("outcome" in p for p in self.problems({"outcome": "nope"})))

    def test_owner_may_ask_in_a_test_channel(self):
        self.assertTrue(any("owner" in p for p in self.problems(self.good_draft(key="C9:1.1", asker="U0OWNER"))))
        self.set_policy("test_channels", "[C9]")
        self.assertEqual(self.problems(self.good_draft(key="C9:1.1", asker="U0OWNER")), [])
        self.assertTrue(any("owner" in p for p in self.problems(self.good_draft(key="C1:1.1", asker="U0OWNER"))))

    def test_window_required_unless_offline(self):
        self.run_cmd("window", "stop")
        self.assertTrue(any("no open window" in p for p in self.problems(self.good_draft())))
        self.assertEqual(self.problems(self.good_draft(), offline=True), [])

    def test_cli_check_reports(self):
        out = self.run_cmd("check", "--json", json.dumps(self.good_draft()))
        self.assertTrue(out["ok"])
        self.assertEqual(out["links"][0]["url"], respond.normalize_url(GDOC))


class TestRecordAndReview(RespondFixture):
    def setUp(self):
        super().setUp()
        self.mark_shared("abc123")
        self.open_window()
        self.run_cmd("tick", "begin")

    def test_record_appends_and_is_idempotent(self):
        out = self.run_cmd("record", "--json", json.dumps(self.good_draft()))
        self.assertTrue(out["recorded"])
        self.assertEqual(out["id"], "R-0001")
        entries = respond.parse_ledger(respond_ledger_path(self.root).read_text())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["outcome"], "answered")
        self.assertEqual(entries[0]["pages"], ["alpha"])
        self.assertEqual(entries[0]["question"], "what is alpha")
        again = self.run_cmd("record", "--json", json.dumps(self.good_draft()))
        self.assertFalse(again["recorded"])
        self.assertTrue(any("already handled" in p for p in again["problems"]))
        self.assertEqual(respond.load_state(self.root)["tick_posts"], 1)

    def test_ignored_goes_to_state_only(self):
        out = self.run_cmd("record", "--json", json.dumps({"outcome": "ignored", "thread_key": "C9:1.1", "reason": "external"}))
        self.assertFalse(out["ledger"])
        state = respond.load_state(self.root)
        self.assertEqual(state["handled"]["C9:1.1"], "ignored:external")
        self.assertEqual(len(respond.parse_ledger(respond_ledger_path(self.root).read_text())), 0)

    def test_clarify_then_answer_follow_up(self):
        policy, _ = respond.load_policy(self.root)
        clar = self.good_draft(outcome="clarified", confidence="medium",
                               message=f"{policy['header']}\nDo you mean the Q4 roadmap?", links=[], pages=[])
        self.assertTrue(self.run_cmd("record", "--json", json.dumps(clar))["recorded"])
        second = self.run_cmd("record", "--json", json.dumps(clar))
        self.assertTrue(any("clarification limit" in p for p in second["problems"]))
        ans = self.run_cmd("record", "--json", json.dumps(self.good_draft()))
        self.assertTrue(ans["recorded"], ans)
        state = respond.load_state(self.root)
        self.assertTrue(state["clarifications"]["C1:100.1"]["resolved"])

    def test_per_tick_cap(self):
        self.set_policy("max_posts_per_tick", "1")
        self.assertTrue(self.run_cmd("record", "--json", json.dumps(self.good_draft()))["recorded"])
        out = self.run_cmd("record", "--json", json.dumps(self.good_draft(key="C1:200.1")))
        self.assertTrue(any("per-tick cap" in p for p in out["problems"]))
        self.run_cmd("tick", "end")
        self.run_cmd("tick", "begin")
        self.assertTrue(self.run_cmd("record", "--json", json.dumps(self.good_draft(key="C1:200.1")))["recorded"])

    def test_reactions_tighten_and_grades(self):
        self.run_cmd("record", "--json", json.dumps(self.good_draft(key="C1:1.1")))
        self.run_cmd("record", "--json", json.dumps(self.good_draft(key="C1:2.1")))
        r1 = self.run_cmd("review", "reaction", "--id", "R-0001", "--value", "-1")
        self.assertFalse(r1["tightened"])
        r2 = self.run_cmd("review", "reaction", "--id", "R-0002", "--value", "-1")
        self.assertTrue(r2["tightened_now"])
        out = self.run_cmd("record", "--json", json.dumps(self.good_draft(key="C1:3.1")))
        self.assertTrue(any("tightened" in p for p in out["problems"]))
        out = self.run_cmd("record", "--json", json.dumps(self.good_draft(key="C1:3.1", house_answer=True)))
        self.assertTrue(out["recorded"])
        g = self.run_cmd("review", "grade", "--id", "R-0001", "--score", "+1", "--note", "good")
        self.assertEqual(g["score"], 1)
        entries = {e["id"]: e for e in respond.parse_ledger(respond_ledger_path(self.root).read_text())}
        self.assertTrue(entries["R-0001"]["grade"].startswith("+1"))
        self.assertEqual(entries["R-0001"]["note"], "good")
        self.assertTrue(entries["R-0002"]["reaction"].startswith("-1"))
        ungraded = self.run_cmd("review", "list")
        self.assertEqual({e["id"] for e in ungraded["entries"]}, {"R-0002", "R-0003"})
        self.run_cmd("record", "--json", json.dumps({"outcome": "ignored", "thread_key": "C1:9.9", "reason": "no-ask"}))
        stats = self.run_cmd("review", "stats")
        self.assertEqual(stats["by_outcome"]["ignored"], 1)
        self.assertEqual(stats["by_outcome"]["answered"], 3)
        self.assertEqual(stats["graded"], 1)
        self.assertEqual(stats["owner_positive_share"], 1.0)
        self.assertEqual(stats["asker_positive_share"], 0.0)
        self.assertFalse(stats["unattended_bar"]["met"])
        self.assertEqual(stats["unattended_bar"]["answered_graded_positive"], 1)
        self.run_cmd("review", "grade", "--id", "R-0002", "--score", "-1")
        stats = self.run_cmd("review", "stats")
        self.assertEqual(stats["answered_with_negative_grade"], ["R-0002"])
        export = self.run_cmd("review", "export", "--graded-only")
        self.assertEqual(export["count"], 2)

    def test_handed_over_recorded_without_message(self):
        out = self.run_cmd("record", "--json", json.dumps(
            {"outcome": "handed-over", "thread_key": "C1:5.5", "channel": "C1 (#t)", "asker": "U1", "question": "q"}))
        self.assertTrue(out["ledger"])
        entries = respond.parse_ledger(respond_ledger_path(self.root).read_text())
        self.assertEqual(entries[0]["outcome"], "handed-over")


class TestWindowAndTick(RespondFixture):
    def test_window_lifecycle(self):
        with self.assertRaises(respond.Refusal):
            self.run_cmd("window", "start", "--until", "2020-01-01T00:00")
        self.open_window(mode="dry")
        with self.assertRaises(respond.Refusal):
            self.open_window()
        st = self.run_cmd("window", "status")
        self.assertTrue(st["open"])
        self.assertFalse(st["past_end"])
        self.assertGreater(st["remaining_seconds"], 0)
        self.run_cmd("tick", "begin")
        self.run_cmd("record", "--json", json.dumps({"outcome": "ignored", "thread_key": "C1:4.4", "reason": "no-ask"}))
        self.run_cmd("record", "--json", json.dumps(
            {"outcome": "handed-over", "thread_key": "C1:5.5", "channel": "C1 (#t)", "asker": "U1", "question": "q"}))
        self.run_cmd("tick", "end")
        self.run_cmd("window", "update", "--task-id", "task-42")
        self.assertEqual(self.run_cmd("window", "status")["window"]["task_id"], "task-42")
        later = (datetime.now().astimezone() + timedelta(hours=5)).isoformat(timespec="seconds")
        self.run_cmd("window", "update", "--until", later)
        self.assertGreater(self.run_cmd("window", "status")["remaining_seconds"], 4 * 3600)
        with self.assertRaises(respond.Refusal):
            self.run_cmd("window", "update", "--until", "2020-01-01T00:00")
        out = self.run_cmd("window", "stop")
        self.assertTrue(out["stopped"])
        self.assertEqual(out["task_id"], "task-42")
        handoff = self.root / out["handoff"]
        self.assertTrue(handoff.is_file())
        self.assertIn("type: respond-handoff", handoff.read_text())
        self.assertIn("- ignored: 1", handoff.read_text())
        self.assertIn("C1:5.5", handoff.read_text())
        self.assertFalse(self.run_cmd("window", "status")["open"])
        self.open_window()  # reopening after a stop is fine

    def test_tick_begin_and_end(self):
        self.assertEqual(self.run_cmd("tick", "begin")["action"], "exit")
        self.open_window()
        first = self.run_cmd("tick", "begin")
        self.assertEqual(first["action"], "run")
        self.assertEqual(first["owner_slack_id"], "U0OWNER")
        self.assertEqual(first["mode"], "live")
        self.run_cmd("tick", "end")
        second = self.run_cmd("tick", "begin")
        since = datetime.fromisoformat(second["since_iso"])
        last = datetime.fromisoformat(self.run_cmd("window", "status")["last_tick"])
        self.assertLessEqual(since, last)
        replay = self.run_cmd("tick", "begin", "--since", "7d")
        self.assertLess(datetime.fromisoformat(replay["since_iso"]), datetime.now().astimezone() - timedelta(days=6))
        state = respond.load_state(self.root)
        state["window"]["until"] = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec="seconds")
        respond.save_state(self.root, state)
        self.assertEqual(self.run_cmd("tick", "begin")["action"], "stop")

    def test_lessons_summary_ignores_fenced_examples(self):
        self.assertEqual(respond.lessons_summary(self.root)["sections"]["House answers"], 0)

    def test_escalate_dry_run(self):
        out = self.run_cmd("escalate", "--text", "ping", "--route", "push,imessage", "--dry-run")
        self.assertTrue(out["all_ok"])
        self.assertEqual([r["route"] for r in out["routes"]], ["push", "imessage"])
        live = self.run_cmd("escalate", "--text", "ping", "--route", "imessage")
        self.assertFalse(live["all_ok"])  # imessage_to is empty


class TestExport(RespondFixture):
    def setUp(self):
        super().setUp()
        self.mark_shared("abc123")
        self.kit = Path(self.td.name) / "kit"

    def export(self, *extra):
        return self.run_cmd("export", "--out", str(self.kit), "--test-channel-name", "#standin-test", *extra)

    def kit_check(self, draft):
        import subprocess
        script = self.kit / "compathy-respond" / "scripts" / "respond.py"
        proc = subprocess.run([sys.executable, str(script), "--target", str(self.kit / "compathy-respond" / "project"),
                               "check", "--offline", "--json", json.dumps(draft)],
                              capture_output=True, text=True, cwd=str(self.kit), check=False)
        return json.loads(proc.stdout)

    def test_kit_holds_only_the_corpus_and_the_rules(self):
        out = self.export()
        self.assertEqual(out["pages"], 4)
        skill = self.kit / "compathy-respond"
        wiki = skill / "project" / "context" / "wiki"
        pages = {p.stem for p in wiki.rglob("*.md") if p.name != "index.md"}
        self.assertEqual(pages, {"alpha", "roadmap", "spec", "technical-patterns"})
        index = (wiki / "index.md").read_text()
        self.assertNotIn("jane-doe", index)
        self.assertNotIn("slack-threads", index)
        self.assertIn("[[alpha]]", index)
        for name in ("respond.py", "compathy_query.py", "lint.py", "lineage.py", "paths.py"):
            self.assertTrue((skill / "scripts" / name).is_file(), name)
        names = {p.name for p in self.kit.rglob("*")}
        for private in ("ledger.md", ".respond-state.json", "log.md", "raw"):
            self.assertNotIn(private, names)
        policy = (skill / "project" / "context" / "respond" / "policy.md").read_text()
        self.assertIn("link_verify_max_age_days: 30", policy)
        self.assertIn('escalation_channel: "#bill-standin-log"', policy)
        self.assertNotIn("Review checklist", policy)
        for rel in ("SETUP.md", "agent-instructions.md", "schedule-instructions.md", "compathy-respond/SKILL.md"):
            text = (self.kit / rel).read_text()
            self.assertNotIn("{{", text, rel)
            self.assertNotIn("\u2014", text, rel)
        self.assertIn("#standin-test", (self.kit / "SETUP.md").read_text())
        files = self.kit / "files"
        self.assertEqual({p.name for p in files.iterdir()}, {"wiki.md", "policy.md", "lessons.md", "sources.md"})
        wiki_text = (files / "wiki.md").read_text()
        for slug in ("alpha", "roadmap", "spec", "technical-patterns"):
            self.assertIn(f"<!-- page: {slug} |", wiki_text)
        for slug in ("jane-doe", "slack-threads"):
            self.assertNotIn(f"<!-- page: {slug} |", wiki_text)
        self.assertNotIn("type: concept", wiki_text)  # frontmatter stripped
        self.assertIn("link_verify_max_age_days: 30", (files / "policy.md").read_text())
        instructions = (self.kit / "agent-instructions.md").read_text()
        self.assertLess(len(instructions), 8000)
        self.assertIn("wiki.md", instructions)
        self.assertIn("<@U0OWNER>", instructions)
        self.assertIn("name: compathy-respond", (skill / "SKILL.md").read_text())
        import zipfile
        with zipfile.ZipFile(self.kit / "compathy-respond.zip") as z:
            listed = z.namelist()
        self.assertIn("compathy-respond/SKILL.md", listed)
        self.assertFalse(any("ledger" in n or "jane-doe" in n for n in listed))
        manifest = json.loads((self.kit / "MANIFEST.json").read_text())
        self.assertEqual(manifest["kind"], "compathy-respond-export")
        self.assertEqual(manifest["pages"], ["alpha", "roadmap", "spec", "technical-patterns"])

    def test_kit_check_runs_from_inside_the_kit(self):
        self.export()
        good = self.good_draft()
        self.assertEqual(self.kit_check(good)["problems"], [])
        self.assertTrue(any("not in the corpus" in p for p in self.kit_check(self.good_draft(pages=["jane-doe"]))["problems"]))
        bad = self.good_draft(links=[CLAUDE])
        bad["message"] = bad["message"].replace(GDOC, CLAUDE)
        self.assertTrue(any("not in the registry" in p for p in self.kit_check(bad)["problems"]))

    def test_export_refuses_unsafe_destinations(self):
        with self.assertRaises(respond.Refusal):
            self.run_cmd("export", "--out", str(self.root / "context" / "kit"))
        other = Path(self.td.name) / "other"
        other.mkdir()
        (other / "keep.txt").write_text("mine")
        with self.assertRaises(respond.Refusal):
            self.run_cmd("export", "--out", str(other), "--force")
        self.assertTrue((other / "keep.txt").is_file())
        self.export()
        with self.assertRaises(respond.Refusal):
            self.export()
        self.assertEqual(self.export("--force")["pages"], 4)

    def test_export_stops_when_the_policy_lacks_the_owner(self):
        self.set_policy("owner_slack_id", "")
        with self.assertRaises(respond.Refusal):
            self.export()
        self.assertFalse(self.kit.exists())


class TestImportedRecords(RespondFixture):
    def test_imported_lines_skip_the_gate_once(self):
        line = {"outcome": "answered", "intent": "feature", "confidence": "high", "thread_key": "C5:10.1",
                "reply_ts": "10.2", "pages": ["jane-doe"], "links": [CLAUDE], "asker": "U9",
                "question": "what is alpha", "channel": "C5", "mode": "chatgpt"}
        out = self.run_cmd("record", "--imported", "--json", json.dumps(line))
        self.assertTrue(out["recorded"])
        again = self.run_cmd("record", "--imported", "--json", json.dumps(line))
        self.assertTrue(again["skipped"])
        entries = respond.parse_ledger(respond_ledger_path(self.root).read_text())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["mode"], "chatgpt")
        self.assertEqual(respond.load_state(self.root)["tick_posts"], 0)
        refused = self.run_cmd("record", "--json", json.dumps(dict(line, thread_key="C5:11.1")))
        self.assertFalse(refused["recorded"])


class TestLintRespond(RespondFixture):
    def test_lint_warns_on_registry_and_is_silent_without_folder(self):
        result = lint.lint(self.root)
        kinds = {w["kind"] for w in result["warnings"]}
        self.assertNotIn("respond-registry-status", kinds)
        self.mark_shared("abc123", verified="")
        p = respond_sources_path(self.root)
        p.write_text(p.read_text().replace("| unknown |", "| weird |", 1))
        kinds = {w["kind"] for w in lint.lint(self.root)["warnings"]}
        self.assertIn("respond-registry-status", kinds)
        self.assertIn("respond-registry-unverified", kinds)
        # an escaped pipe in a title is one cell, not a column shift
        self.run_cmd("sources", "set", "--url", GDOC, "--status", "shared", "--title", "A \\| B", "--force")
        self.run_cmd("sources", "set", "--url", CLAUDE, "--status", "private", "--title", "C | D", "--force")
        kinds = {w["kind"] for w in lint.lint(self.root)["warnings"]}
        self.assertNotIn("respond-registry-url", kinds)
        import shutil
        shutil.rmtree(respond_dir(self.root))
        kinds = {w["kind"] for w in lint.lint(self.root)["warnings"]}
        self.assertFalse(any(k.startswith("respond-") for k in kinds))


if __name__ == "__main__":
    unittest.main()
