"""Publishing a core subset of a wiki, and teammates who link to it.

Owner side: visibility gate, private sections, link rewriting, frontmatter
allowlist, leak scan, core build, commits, personas, push guard, bundle.
Teammate side: import from a layer source or a bundle (mirror), the watch
hook, freshness, and update's page-level report.

Real git against local file:// remotes and bundles; no network.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import layers  # noqa: E402
import lineage as lin  # noqa: E402
import lint  # noqa: E402
import paths  # noqa: E402
import persona  # noqa: E402
import persona_integration  # noqa: E402
import publish  # noqa: E402
import scaffold  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "Owner", "GIT_AUTHOR_EMAIL": "owner@acme.com",
           "GIT_COMMITTER_NAME": "Owner", "GIT_COMMITTER_EMAIL": "owner@acme.com"}
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # fake, shaped like a GitHub token


def git(repo: Path, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                          check=True).stdout.strip()


def page(path: Path, title: str, body: str, ptype: str = "concept", **fm) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = "".join(f"{k}: {v}\n" for k, v in fm.items())
    path.write_text(f"---\ntype: {ptype}\nschema_version: 1\ncreated: 2026-11-01\nupdated: 2026-11-01\n"
                    f"sources: [contract.pdf]\nrelated_paths: [/Users/owner/notes]\n{extra}---\n"
                    f"# {title}\n\n{body}\n", encoding="utf-8")


def add_index(target: Path, section: str, entry: str) -> None:
    idx = paths.index_path(target)
    # pylint: disable=protected-access
    idx.write_text(persona_integration._insert_index_entry(idx.read_text(), section=section, entry=entry))


def run(module, argv) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = module.main(argv)
    out = buf.getvalue().strip()
    data = json.loads(out) if out.startswith("{") else {"_text": out}
    data["_exit"] = code
    return data


class Env(unittest.TestCase):
    """Isolated state home, fake HOME, git identity."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        home = self.root / "home"
        home.mkdir()
        env = dict(GIT_ENV)
        env[paths.STATE_HOME_ENV] = str(self.root / "state")
        env["HOME"] = str(home)
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        pj = mock.patch.object(persona_integration, "PERSONA_JSON_PATH", home / "nope.json")
        pj.start()
        self.addCleanup(pj.stop)
        self.addCleanup(lambda: layers.remove_tree(self.root / "state"))
        self.addCleanup(self.td.cleanup)

    def owner(self, name="owner") -> Path:
        """An owner wiki: two org pages, a team page, a private page, a private section."""
        t = self.root / name
        t.mkdir()
        scaffold.create_structure(t, "Acme Pricing")
        w = paths.wiki_dir(t)
        page(w / "concepts" / "pricing.md", "Pricing",
             "We sell by [[tiers]]. Owned by [[jane-doe]]. See [[secret-deal|the big deal]].\n\n"
             "<!-- private -->\nInternal margin is 42 percent.\n<!-- /private -->\n\n"
             "Code `[[not-a-link]]` stays.", visibility="org")
        page(w / "concepts" / "tiers.md", "Tiers", "Three tiers. Write to ops@acme.com.", visibility="org")
        page(w / "concepts" / "roadmap.md", "Roadmap", "Ships in Q1.", visibility="team")
        page(w / "entities" / "jane-doe.md", "Jane Doe", "Head of sales.", ptype="entity")
        page(w / "concepts" / "secret-deal.md", "Secret deal", "Codename Bluebird.")
        add_index(t, "## Concepts", "- [[pricing]]: how we price, see [[secret-deal]]")
        add_index(t, "## Concepts", "- [[tiers]]: the three tiers")
        add_index(t, "## Concepts", "- [[roadmap]]: when things ship")
        add_index(t, "## Concepts", "- [[secret-deal]]: hush")
        add_index(t, "## Entities", "- [[jane-doe]]: head of sales")
        assert lint.lint(t)["summary"]["errors"] == 0
        return t

    def init(self, owner: Path, remote: bool = True, bundle: bool = True, **extra) -> dict:
        argv = ["init", "--target", str(owner), "--layer-id", "acme/pricing", "--title", "Acme pricing core",
                "--repo", str(self.root / "core"), "--owner-name", "Owner", "--owner-email", "owner@acme.com",
                "--owner-github", "owner", "--email-domain", "acme.com"]
        if remote:
            self.bare = self.root / "github" / "pricing-core.git"
            self.bare.parent.mkdir(parents=True, exist_ok=True)
            git(self.bare.parent, "init", "-q", "--bare", "-b", "main", str(self.bare))
            argv += ["--remote", "git+file://" + str(self.bare)]
        if bundle:
            (self.root / "drive").mkdir(exist_ok=True)
            argv += ["--bundle", str(self.root / "drive" / "pricing-core.bundle")]
        r = run(publish, argv)
        self.assertEqual(r["_exit"], 0, r)
        cfg = publish.load_config(owner)
        cfg["scan"]["deny"] = ["bluebird"]
        cfg["personas"] = {"marketing": {"title": "Pricing marketing", "summary": "Messaging for pricing.",
                                         "tags": ["marketing"], "reads_first": ["pricing"],
                                         "responsibilities": ["campaign copy"]}}
        cfg["allow_local_source"] = True  # the fixtures' "remote" is a local bare repo
        cfg.update(extra)
        lin.write_json_atomic(publish.config_path(owner), cfg)
        return cfg


# ---------- pure transforms ----------

class TestTransforms(unittest.TestCase):
    def test_strip_private_blocks_outside_code(self):
        body = "a\n<!-- private -->\nsecret\n<!-- /private -->\nb\n```\n<!-- private -->\n```\n`<!-- /private -->`\n"
        out, n = publish.strip_private(body)
        self.assertEqual(n, 1)
        self.assertNotIn("secret", out)
        self.assertIn("```\n<!-- private -->\n```", out)  # markers inside code are text

    def test_strip_private_fails_closed(self):
        for bad in ("<!-- private -->\nnever closed", "stray <!-- /private -->",
                    "<!-- private --> <!-- private --> <!-- /private -->"):
            with self.assertRaises(ValueError):
                publish.strip_private(bad)

    def test_private_block_spanning_code_is_removed(self):
        out, _ = publish.strip_private("x\n<!-- private -->\n```\nkey\n```\n<!-- /private -->\ny")
        self.assertNotIn("key", out)

    def test_rewrite_links(self):
        body = "[[keep]] [[gone]] [[gone|label]] [[other]] `[[gone]]`"
        out, unlinked = publish.rewrite_links(body, {"keep"}, {"gone": "Gone Page"})
        self.assertEqual(out, "[[keep]] Gone Page label other `[[gone]]`")
        self.assertEqual(unlinked, ["gone", "gone", "other"])

    def test_frontmatter_allowlist(self):
        kept, dropped = publish.filter_frontmatter(
            ["type: concept\n", "sources: [a.pdf]\n", "related_paths: [x/]\n", "visibility: org\n",
             "# comment\n", "owner_notes: private\n", "aliases: [a, b]\n"])
        self.assertEqual("".join(kept), "type: concept\nvisibility: org\naliases: [a, b]\n")
        self.assertEqual(sorted(dropped), ["owner_notes", "related_paths", "sources"])


class TestScan(unittest.TestCase):
    CFG = {"scan": {"deny": ["bluebird", r"\bproject x\b"], "email_domains": ["acme.com"], "allow": []}}

    def rules(self, text):
        return [f["rule"] for f in publish.scan_text("f.md", text, self.CFG)]

    def test_rules(self):
        self.assertEqual(self.rules("Codename BLUEBIRD."), ["deny:0"])
        self.assertEqual(self.rules("about Project X today"), ["deny:1"])
        self.assertEqual(self.rules(f"token {TOKEN}"), ["secret:github-token"])
        self.assertEqual(self.rules("ssn 123-45-6789"), ["pii:ssn"])
        self.assertEqual(self.rules("call (555) 123-4567"), ["pii:phone"])
        self.assertEqual(self.rules("see /Users/jane/Desktop/x"), ["path:home"])
        self.assertEqual(self.rules("password = hunter2hunter2"), ["secret:assignment"])
        self.assertEqual(self.rules("ops@acme.com and a@sub.acme.com"), [])
        self.assertEqual(self.rules("bob@gmail.com"), ["pii:email"])
        self.assertEqual(self.rules("NDC 12345-678-90 on 2026-10-01, $1,234"), [])

    def test_matched_text_never_reported(self):
        findings = publish.scan_text("f.md", f"x {TOKEN} bluebird bob@gmail.com", self.CFG)
        blob = json.dumps(findings)
        for secret in (TOKEN, "bluebird", "bob@gmail.com"):
            self.assertNotIn(secret, blob)
        self.assertTrue(all(len(f["fingerprint"]) == 12 for f in findings))

    def test_allow_by_rule_file_and_fingerprint(self):
        findings = publish.scan_text("a.md", "bob@gmail.com, sue@gmail.com", self.CFG)
        fp = findings[0]["fingerprint"]
        cfg = {"scan": {"allow": [{"rule": "pii:email", "fingerprint": fp, "reason": "partner"},
                                  {"rule": "secret:*", "reason": "never used"}]}}
        blocking, allowed, unused = publish.apply_allow(findings, cfg)
        self.assertEqual(len(blocking), 1)
        self.assertEqual(allowed[0]["reason"], "partner")
        self.assertEqual(unused, [{"rule": "secret:*", "reason": "never used"}])
        cfg = {"scan": {"allow": [{"rule": "pii:*", "file": "a.md", "reason": "all fine here"}]}}
        self.assertEqual(publish.apply_allow(findings, cfg)[0], [])

    def test_config_validation(self):
        good = {"schema_version": 1, "layer": {"id": "acme/pricing", "role": "team", "title": "T"},
                "repo": "/tmp/x"}
        self.assertEqual(publish.validate_config(good), [])
        bad = dict(good, audience="everyone", scan={"deny": ["("], "allow": [{"rule": "pii:email"}]},
                   bundle="drive/core.zip", remote="git+https://x-token:abc@github.com/acme/core.git")
        probs = " ".join(publish.validate_config(bad))
        for needle in ("audience", "scan.deny[0]", "scan.allow[0] needs a 'reason'", "bundle", "credentials"):
            self.assertIn(needle, probs)

    def test_same_remote(self):
        same = publish.same_remote
        self.assertTrue(same("git+https://github.com/acme/core.git", "git+ssh://git@github.com/acme/core"))
        self.assertTrue(same("git+file:///tmp/a/core.git", "git+file:///tmp/a/core.git/"))
        self.assertTrue(same(publish.layers_mod.git_url_to_source("git@GitHub.com:acme/core.git"),
                             "git+https://github.com/acme/core.git"))
        self.assertFalse(same("git+https://github.com/acme/core.git", "git+https://github.com/acme/other.git"))
        self.assertFalse(same(None, "git+https://github.com/acme/core.git"))

    def test_public_remote_guard_forms(self):
        self.assertEqual(publish.anonymous_readable("git+file:///tmp/x.git")["public"], False)
        self.assertIsNone(publish.anonymous_readable("git+ssh://git@git.internal/a/b.git")["public"])
        self.assertEqual(publish._anonymous_url("git+ssh://git@github.com/acme/core.git"),  # pylint: disable=protected-access
                         "https://github.com/acme/core.git")


# ---------- owner side ----------

class TestPublish(Env):
    def test_plan_default_deny_and_audience(self):
        owner = self.owner()
        self.init(owner)
        p = publish.plan(owner, publish.load_config(owner))
        p.pop("_build")
        self.assertEqual([x["slug"] for x in p["published"]], ["pricing", "tiers"])
        self.assertEqual(p["pages"]["private"], 3)  # roadmap (team), jane-doe, secret-deal
        self.assertEqual(p["unlinked"], [{"page": "pricing", "targets": ["jane-doe", "secret-deal"]}])
        self.assertEqual(p["private_blocks"], [{"page": "pricing", "blocks": 1}])
        self.assertEqual(p["dropped_frontmatter"], {"sources": 2, "related_paths": 2})
        self.assertTrue(p["ok"], p["blockers"])
        cfg = publish.load_config(owner)
        cfg["audience"] = "team"
        p = publish.plan(owner, cfg)
        self.assertIn("roadmap", [x["slug"] for x in p["published"]])

    def test_apply_writes_core_and_pins_personas(self):
        owner = self.owner()
        self.init(owner)
        r = run(publish, ["apply", "--target", str(owner), "--note", "first publish"])
        self.assertEqual(r["_exit"], 0, r)
        core = self.root / "core"
        self.assertEqual(git(core, "log", "--format=%s"), f"personas: pin {r['pin'][:12]}\npublish: first publish")
        files = git(core, "ls-files").splitlines()
        self.assertEqual(sorted(files), [".github/CODEOWNERS", "README.md", "context/personas/index.json",
                                         "context/personas/marketing.json", "context/schema.md",
                                         "context/wiki/concepts/pricing.md", "context/wiki/concepts/tiers.md",
                                         "context/wiki/index.md", "context/wiki/log.md"])
        text = (core / "context/wiki/concepts/pricing.md").read_text()
        self.assertIn("Owned by Jane Doe. See the big deal.", text)
        for gone in ("42 percent", "sources:", "related_paths", "/Users/owner", "[[jane-doe]]"):
            self.assertNotIn(gone, text)
        self.assertIn("`[[not-a-link]]`", text)
        index = (core / "context/wiki/index.md").read_text()
        self.assertIn("- [[pricing]]: how we price, see Secret deal", index)
        self.assertNotIn("roadmap", index)
        self.assertIn("## [", (core / "context/wiki/log.md").read_text())
        self.assertEqual(lint.lint(core)["summary"]["errors"], 0)
        m = lin.read_json(core / "context/personas/marketing.json")
        self.assertEqual(lin.validate_manifest(m), [])
        self.assertEqual(m["layers"][0]["pin"], r["content_commit"])
        self.assertEqual(m["layers"][0]["tree_sha"], git(core, "rev-parse", f"{r['content_commit']}:context/wiki"))
        self.assertEqual(m["layers"][0]["source"], "git+file://" + str(self.bare))
        self.assertEqual(m["reads_first"], ["acme/pricing/pricing"])
        self.assertIn("publish | acme/pricing at", paths.log_path(owner).read_text())
        readme = (core / "README.md").read_text()
        self.assertIn(f"/compathy-persona import git+file://{self.bare}#marketing", readme)
        self.assertIn('import "<path to pricing-core.bundle>#marketing"', readme)
        # Nothing changed: nothing committed.
        again = run(publish, ["apply", "--target", str(owner)])
        self.assertEqual((again["content_commit"], again["personas_commit"]), (None, None))
        self.assertEqual(len(git(core, "log", "--format=%H").splitlines()), 2)

    def test_scan_blocks_and_core_is_untouched(self):
        owner = self.owner()
        self.init(owner)
        p = paths.wiki_dir(owner) / "concepts" / "tiers.md"
        p.write_text(p.read_text().replace("Three tiers.", f"Three tiers, project Bluebird, key {TOKEN}."))
        r = run(publish, ["plan", "--target", str(owner)])
        self.assertFalse(r["ok"])
        self.assertEqual(sorted(f["rule"] for f in r["findings"]), ["deny:0", "secret:github-token"])
        self.assertNotIn(TOKEN, json.dumps(r))
        # The published copy lost two frontmatter lines; the source line is where to fix it.
        src_line = next(i for i, l in enumerate(p.read_text().splitlines(), start=1) if "Bluebird" in l)
        for f in r["findings"]:
            self.assertEqual(f["source"], {"file": "wiki/concepts/tiers.md", "line": src_line})
            self.assertEqual(f["line"], src_line - 2)
        r = run(publish, ["apply", "--target", str(owner)])
        self.assertEqual(r["_exit"], 1)
        self.assertIn("publish blocked", r["error"])
        self.assertFalse((self.root / "core" / "context").exists())

    def test_unbalanced_private_marker_blocks(self):
        owner = self.owner()
        self.init(owner)
        p = paths.wiki_dir(owner) / "concepts" / "tiers.md"
        p.write_text(p.read_text() + "\n<!-- private -->\nleft open\n")
        r = run(publish, ["plan", "--target", str(owner)])
        self.assertFalse(r["ok"])
        self.assertTrue(any(x.get("blocking") for x in r["page_problems"]))

    def test_no_git_identity_uses_owner_and_failure_rolls_back(self):
        owner = self.owner()
        self.init(owner)
        core = self.root / "core"
        bare_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        bare_env["GIT_CONFIG_NOSYSTEM"] = "1"
        bare_env["EMAIL"] = ""
        with mock.patch.dict(os.environ, bare_env, clear=True):
            r = run(publish, ["apply", "--target", str(owner)])
        self.assertEqual(r["_exit"], 0, r)
        self.assertEqual(git(core, "log", "-1", "--format=%an <%ae>"), "Owner <owner@acme.com>")
        # A failing commit leaves the core repo exactly as it was.
        p = paths.wiki_dir(owner) / "concepts" / "tiers.md"
        p.write_text(p.read_text().replace("Three tiers.", "Four tiers."))
        head = git(core, "rev-parse", "HEAD")
        real = publish._commit  # pylint: disable=protected-access

        def boom(*a, **k):
            real(*a, **k)
            raise publish.PublishError("simulated failure after the content commit")

        with mock.patch.object(publish, "_commit", side_effect=boom):
            r = run(publish, ["apply", "--target", str(owner)])
        self.assertEqual(r["_exit"], 1)
        self.assertEqual(git(core, "status", "--porcelain"), "")
        self.assertNotEqual(git(core, "rev-parse", "HEAD"), "")  # still a valid repo
        self.assertEqual(run(publish, ["apply", "--target", str(owner)])["_exit"], 0)
        self.assertNotEqual(git(core, "rev-parse", "HEAD"), head)

    def test_missing_bundle_error(self):
        with self.assertRaises(persona.PersonaError) as cm:
            persona.split_source_arg(str(self.root / "drive" / "nope.bundle") + "#marketing")
        self.assertIn("is the shared drive synced", str(cm.exception))

    def test_dirty_core_repo_refused(self):
        owner = self.owner()
        self.init(owner)
        (self.root / "core" / "stray.txt").write_text("hand edit")
        r = run(publish, ["apply", "--target", str(owner)])
        self.assertEqual(r["_exit"], 1)
        self.assertIn("uncommitted changes", r["error"])

    def test_mark_sets_and_clears(self):
        owner = self.owner()
        r = run(publish, ["mark", "jane-doe", "--visibility", "org", "--target", str(owner)])
        self.assertEqual(r["results"], [{"slug": "jane-doe", "action": "set"}])
        text = (paths.wiki_dir(owner) / "entities" / "jane-doe.md").read_text()
        self.assertIn("schema_version: 1\nvisibility: org\n", text)
        r = run(publish, ["mark", "jane-doe", "--visibility", "private", "--target", str(owner)])
        self.assertEqual(r["results"][0]["action"], "cleared")
        self.assertNotIn("visibility", (paths.wiki_dir(owner) / "entities" / "jane-doe.md").read_text())
        r = run(publish, ["mark", "nope", "--visibility", "org", "--target", str(owner)])
        self.assertEqual(r["_exit"], 1)

    def test_lint_rejects_unknown_visibility(self):
        owner = self.owner()
        page(paths.wiki_dir(owner) / "concepts" / "odd.md", "Odd", "x", visibility="everyone")
        add_index(owner, "## Concepts", "- [[odd]]: odd")
        kinds = [e["kind"] for e in lint.lint(owner)["errors"]]
        self.assertIn("invalid-visibility", kinds)

    def test_push_guard_bundle_and_remote(self):
        owner = self.owner()
        self.init(owner)
        self.assertEqual(run(publish, ["apply", "--target", str(owner)])["_exit"], 0)
        with mock.patch.object(publish, "anonymous_readable",
                               return_value={"public": True, "reason": "answers without credentials"}):
            r = run(publish, ["push", "--target", str(owner)])
        self.assertEqual(r["_exit"], 1)
        self.assertIn("refusing to push", r["error"])
        with mock.patch.object(publish, "anonymous_readable",
                               return_value={"public": None, "reason": "unknown host"}):
            self.assertIn("--confirm-private", run(publish, ["push", "--target", str(owner)])["error"])
        r = run(publish, ["push", "--target", str(owner)])
        self.assertEqual(r["_exit"], 0, r)
        head = git(self.root / "core", "rev-parse", "HEAD")
        self.assertEqual(git(self.bare, "rev-parse", "main"), head)
        bundle = self.root / "drive" / "pricing-core.bundle"
        self.assertEqual(r["bundle"]["head"], head)
        subprocess.run(["git", "bundle", "verify", str(bundle)], cwd=str(self.root / "core"),
                       check=True, capture_output=True)
        st = run(publish, ["status", "--target", str(owner)])
        self.assertEqual(st["bundle"]["state"], "current")
        self.assertEqual(st["visibility"], {"org": 2, "team": 1, "private": 2})
        self.assertNotIn("bluebird", json.dumps(st))  # deny patterns are counted, never shown


# ---------- teammate side ----------

class TestTeammate(Env):
    def publish_core(self) -> Path:
        owner = self.owner()
        self.init(owner)
        self.assertEqual(run(publish, ["apply", "--target", str(owner), "--note", "first"])["_exit"], 0)
        self.assertEqual(run(publish, ["push", "--target", str(owner)])["_exit"], 0)
        return owner

    def teammate(self) -> Path:
        t = self.root / "jane"
        t.mkdir()
        return t

    def test_split_source_arg(self):
        b = self.root / "x.bundle"
        b.write_text("not really a bundle")
        self.assertEqual(persona.split_source_arg("git+https://h/a.git#sales"), ("git+https://h/a.git", "sales"))
        self.assertEqual(persona.split_source_arg("https://h/a.git"), ("git+https://h/a.git", None))
        self.assertEqual(persona.split_source_arg(f"{b}#design"), ("git+file://" + str(b.resolve()), "design"))
        self.assertIsNone(persona.split_source_arg("acme/pricing/marketing"))
        self.assertIsNone(persona.split_source_arg("https://h/persona.json"))
        with self.assertRaises(persona.PersonaError):
            persona.split_source_arg("git+https://h/a.git#Not A Role")

    def test_import_from_bundle_records_mirror_and_hook(self):
        self.publish_core()
        jane = self.teammate()
        bundle = self.root / "drive" / "pricing-core.bundle"
        r = run(persona, ["import", str(bundle), "--target", str(jane)])
        self.assertIn("#marketing (Pricing marketing)", r["error"])
        r = run(persona, ["search", "--source", str(bundle), "--target", str(jane)])
        self.assertEqual([e["id"] for e in r["results"]], ["acme/pricing/marketing"])
        r = run(persona, ["import", f"{bundle}#marketing", "--target", str(jane)])
        self.assertEqual(r["source_kind"], "source")
        self.assertEqual(r["mirror"]["canonical"], "git+file://" + str(self.bare))
        r = run(persona, ["import", f"{bundle}#marketing", "--target", str(jane), "--apply", "--allow-local-source",
                          "--consent", "fetch,link,watch", "--project-name", "jane"])
        self.assertEqual(r["_exit"], 0, r)
        self.assertEqual(r["trust"], 3)
        self.assertTrue(r["fetched"][0]["via"].endswith("pricing-core.bundle"))
        self.assertEqual(layers.load_mirrors(), {"git+file://" + str(self.bare): "git+file://" + str(bundle.resolve())})
        doc = lin.load_lineage(jane)
        self.assertEqual(doc["layers"][0]["source"], "git+file://" + str(self.bare))  # canonical, shareable
        hook = json.loads((jane / ".claude" / "settings.local.json").read_text())
        cmd = hook["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn("persona.py", cmd)
        self.assertIn("freshness", cmd)
        self.assertEqual(r["watch_action"], "installed")
        # Reading the core needs no GitHub access: the layer came from the bundle.
        self.assertTrue(lin.is_layer_cached(doc["layers"][0]))
        u = run(persona, ["unlink", "--target", str(jane)])
        self.assertTrue(any("watch hook" in x for x in u["removed"]))
        self.assertNotIn("hooks", json.loads((jane / ".claude" / "settings.local.json").read_text()))

    def test_freshness_and_update_report(self):
        owner = self.publish_core()
        jane = self.teammate()
        r = run(persona, ["import", f"git+file://{self.bare}#marketing", "--target", str(jane), "--apply", "--allow-local-source",
                          "--consent", "fetch,link", "--project-name", "jane"])
        self.assertEqual(r["_exit"], 0, r)
        page(paths.wiki_dir(jane) / "concepts" / "campaign.md", "Campaign", "Lead with [[pricing]] and [[tiers]].")
        add_index(jane, "## Concepts", "- [[campaign]]: the campaign")
        self.assertEqual(lint.lint(jane)["summary"]["errors"], 0)
        f = run(persona, ["freshness", "--target", str(jane), "--ttl", "0"])
        self.assertEqual([l["state"] for l in f["layers"]], ["current"])  # HEAD is the persona commit
        self.assertIsNone(f["line"])

        # Owner republishes: pricing changes, tiers goes private, bundles arrives.
        w = paths.wiki_dir(owner)
        p = w / "concepts" / "pricing.md"
        p.write_text(p.read_text().replace("We sell by [[tiers]].", "We sell by [[bundles]]."))
        page(w / "concepts" / "bundles.md", "Bundles", "Two bundles.", visibility="org")
        add_index(owner, "## Concepts", "- [[bundles]]: two bundles")
        run(publish, ["mark", "tiers", "--visibility", "private", "--target", str(owner)])
        self.assertEqual(run(publish, ["apply", "--target", str(owner), "--note", "bundles replace tiers"])["_exit"], 0)
        self.assertEqual(run(publish, ["push", "--target", str(owner)])["_exit"], 0)

        cached = run(persona, ["freshness", "--target", str(jane)])  # within the TTL: no network
        self.assertTrue(cached["layers"][0]["from_cache"])
        f = run(persona, ["freshness", "--target", str(jane), "--ttl", "0"])
        self.assertEqual(f["behind"], ["acme/pricing"])
        self.assertIn("bundles replace tiers", f["line"])
        line = run(persona, ["freshness", "--target", str(jane), "--ttl", "0", "--format", "line"])
        self.assertTrue(line["_text"].startswith("compathy: acme/pricing has a newer publish"))

        u = run(persona, ["update", "--target", str(jane)])
        ch = u["changes"][0]
        self.assertEqual(ch["status"], "changed")
        self.assertEqual([p["slug"] for p in ch["changed_pages"]["added"]], ["bundles"])
        self.assertEqual([p["slug"] for p in ch["changed_pages"]["modified"]], ["pricing"])
        self.assertEqual([p["slug"] for p in ch["changed_pages"]["removed"]], ["tiers"])
        self.assertEqual(ch["publish_notes"][0]["entry"].split("| ", 1)[1], "bundles replace tiers")
        self.assertEqual(ch["affected"][0]["slug"], "campaign")  # the seeded persona page is skipped
        self.assertEqual(ch["affected"][0]["links_to_removed"], ["tiers"])
        self.assertEqual(len(ch["affected"]), 1)
        u = run(persona, ["update", "--target", str(jane), "--apply"])
        self.assertTrue(u["applied"])
        self.assertIn("broken-backlink", [e["kind"] for e in lint.lint(jane)["errors"]])
        self.assertIn("review here: campaign", paths.log_path(jane).read_text())
        f = run(persona, ["freshness", "--target", str(jane), "--ttl", "0", "--format", "line"])
        self.assertEqual(f["_text"], "")

    def test_readme_only_commit_is_not_news(self):
        owner = self.publish_core()
        jane = self.teammate()
        run(persona, ["import", f"git+file://{self.bare}#marketing", "--target", str(jane), "--apply", "--allow-local-source",
                      "--consent", "fetch,link", "--project-name", "jane"])
        cfg = publish.load_config(owner)
        cfg["owner"]["email"] = "pricing-owner@acme.com"  # README and personas change, pages do not
        lin.write_json_atomic(publish.config_path(owner), cfg)
        r = run(publish, ["apply", "--target", str(owner)])
        self.assertTrue(r["content_commit"])
        self.assertEqual(run(publish, ["push", "--target", str(owner)])["_exit"], 0)
        f = run(persona, ["freshness", "--target", str(jane), "--ttl", "0"])
        self.assertEqual(f["layers"][0]["state"], "current")
        u = run(persona, ["update", "--target", str(jane)])
        self.assertEqual(u["changes"][0]["status"], "same-content")



# ---------- online source of truth ----------

class TestOnlineSource(Env):
    """Personas pin online sources; owners are told to create one; adopters are guarded."""

    def git_wiki(self, name="team") -> Path:
        t = self.root / name
        t.mkdir()
        git(t, "init", "-q", "-b", "main")
        scaffold.create_structure(t, name)
        git(t, "add", "-A")
        git(t, "commit", "-qm", "wiki")
        return t

    def test_is_online_source(self):
        self.assertTrue(lin.is_online_source("git+https://github.com/acme/core.git"))
        self.assertTrue(lin.is_online_source("git+ssh://git@github.com/acme/core.git"))
        self.assertFalse(lin.is_online_source("git+file:///tmp/core.git"))
        self.assertFalse(lin.is_online_source(None))

    def test_export_prompts_owner_for_an_online_source(self):
        t = self.git_wiki()
        base = ["export", "write", "--target", str(t), "--role", "analyst", "--layer-id", "acme/team",
                "--title", "Analyst", "--exported-by", "lead@acme.com"]
        r = run(persona, base)  # no origin at all
        self.assertEqual((r["_exit"], r["needs"]), (1, "online-source"))
        self.assertIn("gh repo create", " ".join(r["how"]))
        r = run(persona, base + ["--source", "git+file://" + str(t)])  # a local path
        self.assertEqual(r["needs"], "online-source")
        r = run(persona, ["export", "propose", "--target", str(t), "--role", "analyst", "--layer-id", "acme/team"])
        self.assertFalse(r["preconditions"]["online_source"])
        self.assertEqual(r["needs"], "online-source")
        git(t, "remote", "add", "origin", "https://github.com/acme/team-wiki.git")
        r = run(persona, base)
        self.assertEqual(r["_exit"], 0, r)
        self.assertEqual(r["persona"]["layers"][0]["source"], "git+https://github.com/acme/team-wiki.git")

    def test_import_refuses_a_persona_without_an_online_source(self):
        t = self.git_wiki()
        r = run(persona, ["export", "write", "--target", str(t), "--role", "analyst", "--layer-id", "acme/team",
                          "--title", "Analyst", "--exported-by", "lead@acme.com",
                          "--source", "git+file://" + str(t), "--allow-local-source"])
        self.assertEqual(r["_exit"], 0, r)
        member = self.root / "member"
        member.mkdir()
        plan = run(persona, ["import", r["written"], "--target", str(member)])
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["blockers"][0]["layers"], ["acme/team"])
        self.assertIn("Ask lead@acme.com", plan["next"])
        r2 = run(persona, ["import", r["written"], "--target", str(member), "--apply", "--consent", "fetch,link"])
        self.assertEqual((r2["_exit"], r2["needs"], r2["owner"]), (1, "online-source", "lead@acme.com"))
        self.assertFalse(paths.lineage_path(member).exists())
        r3 = run(persona, ["import", r["written"], "--target", str(member), "--apply", "--consent", "fetch,link",
                           "--allow-local-source"])
        self.assertEqual(r3["_exit"], 0, r3)

    def test_publish_needs_an_online_remote(self):
        owner = self.owner()
        r = run(publish, ["init", "--target", str(owner), "--layer-id", "acme/pricing",
                          "--repo", str(self.root / "core"), "--owner-name", "Owner",
                          "--owner-email", "owner@acme.com", "--email-domain", "acme.com"])
        self.assertEqual((r["_exit"], r["needs"]), (0, "online-source"))
        p = publish.plan(owner, publish.load_config(owner))
        self.assertFalse(p["ok"])
        self.assertIn(publish.NO_ONLINE_SOURCE, p["blockers"])
        r = run(publish, ["remote", "git+file:///tmp/somewhere.git", "--target", str(owner)])
        self.assertEqual((r["_exit"], r["needs"]), (1, "online-source"))
        with mock.patch.object(publish, "anonymous_readable",
                               return_value={"public": True, "reason": "answers without credentials"}):
            self.assertIn("private repository", run(publish, ["remote", "https://github.com/acme/core.git",
                                                              "--target", str(owner)])["error"])
        with mock.patch.object(publish, "anonymous_readable",
                               return_value={"public": None, "reason": "unknown host"}):
            self.assertIn("--confirm-private",
                          run(publish, ["remote", "git@git.acme.internal:acme/core.git",
                                        "--target", str(owner)])["error"])
        with mock.patch.object(publish, "anonymous_readable",
                               return_value={"public": False, "reason": "remote requires credentials"}):
            r = run(publish, ["remote", "git@github.com:acme/pricing-core.git", "--target", str(owner)])
        self.assertEqual(r["_exit"], 0, r)
        self.assertEqual(r["remote"], "git+ssh://git@github.com/acme/pricing-core.git")
        self.assertEqual(git(self.root / "core", "remote", "get-url", "origin"), "ssh://git@github.com/acme/pricing-core.git")
        p = publish.plan(owner, publish.load_config(owner))
        self.assertNotIn(publish.NO_ONLINE_SOURCE, p["blockers"])

    def test_online_source_first_mirror_as_fallback(self):
        mirrors = {"git+https://h/a.git": "git+file:///x.bundle", "git+file:///o/core.git": "git+file:///x.bundle"}
        with mock.patch.object(layers, "load_mirrors", return_value=mirrors):
            self.assertEqual(layers.candidate_sources("git+https://h/a.git"),
                             ["git+https://h/a.git", "git+file:///x.bundle"])
            self.assertEqual(layers.candidate_sources("git+file:///o/core.git"),
                             ["git+file:///x.bundle", "git+file:///o/core.git"])
            self.assertEqual(layers.candidate_sources("git+https://other/b.git"), ["git+https://other/b.git"])
        repo = self.git_wiki("src")
        bundle = self.root / "core.bundle"
        git(repo, "bundle", "create", str(bundle), "HEAD", "main")
        canonical = "git+https://127.0.0.1:9/core.git"  # nothing listens there: unreachable at once
        persona._save_mirror(canonical, "git+file://" + str(bundle))  # pylint: disable=protected-access
        self.assertEqual(layers.upstream_head(canonical, timeout=10), git(repo, "rev-parse", "HEAD"))
        self.assertEqual(layers.probe_upstream(canonical, timeout=20)["head"], git(repo, "rev-parse", "HEAD"))



# ---------- Notion copy ----------

class TestNotion(Env):
    """Setup, create, write and record: the copy teammates without git read."""

    ROOT = "https://www.notion.so/Acme-pricing-core-" + "a" * 32
    CONTAINER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    def published(self) -> Path:
        owner = self.owner()
        self.init(owner)
        self.assertEqual(run(publish, ["apply", "--target", str(owner), "--note", "first"])["_exit"], 0)
        return owner

    def linked(self, owner: Path) -> dict:
        """Run setup and create with fake ids; return the slug -> id map."""
        self.assertEqual(run(publish, ["notion-plan", "--target", str(owner)])["phase"], "setup")
        r = run(publish, ["notion-record", "--target", str(owner), "--root", self.ROOT, "--container", self.CONTAINER])
        self.assertEqual(r["recorded"], ["root", "container"])
        plan = run(publish, ["notion-plan", "--target", str(owner)])
        self.assertEqual(plan["phase"], "create")
        ids = {c["slug"]: hashlib.md5(c["slug"].encode()).hexdigest() for c in plan["create"]}
        r = run(publish, ["notion-record", "--target", str(owner), "--ids", json.dumps(ids)])
        self.assertEqual(r["pages"], len(ids))
        return ids

    def test_literal_tags_in_prose_are_escaped_for_notion(self):
        out = publish.notion_links("File by <date> and see `<date>` and x < 5 <!-- note -->", {}, {}, None)
        self.assertEqual(out, "File by \\<date> and see `<date>` and x < 5 ")

    def test_notion_ids(self):
        self.assertEqual(publish.notion_id(self.ROOT), "a" * 32)
        self.assertEqual(publish.notion_id(self.CONTAINER), "b" * 32)
        self.assertEqual(publish.notion_id("https://www.notion.so/x-" + "C" * 32 + "?pvs=4"), "c" * 32)
        self.assertIsNone(publish.notion_id("https://example.com/page"))
        self.assertEqual(publish.notion_id("https://app.notion.com/p/" + "d" * 32 + "?pvs=204"), "d" * 32)

    def test_links_follow_the_url_form_notion_returned(self):
        owner = self.published()
        run(publish, ["notion-record", "--target", str(owner), "--root", "https://app.notion.com/p/" + "e" * 32 + "?pvs=204",
                      "--container", "https://app.notion.com/p/" + "f" * 32])
        plan = run(publish, ["notion-plan", "--target", str(owner)])
        run(publish, ["notion-record", "--target", str(owner), "--ids",
                      json.dumps({c["slug"]: "https://app.notion.com/p/" + hashlib.md5(c["slug"].encode()).hexdigest()
                                  for c in plan["create"]})])
        plan = run(publish, ["notion-plan", "--target", str(owner)])
        root = Path(plan["root_update"]["file"]).read_text()
        self.assertEqual(root.strip().splitlines()[-1], '<page url="https://app.notion.com/p/' + "f" * 32 + '">All pages</page>')
        self.assertTrue(all(u["page"].startswith("https://app.notion.com/p/") for u in plan["updates"]))

    def test_setup_create_write_record_done(self):
        owner = self.published()
        ids = self.linked(owner)
        self.assertEqual(sorted(ids), ["pricing", "tiers"])
        plan = run(publish, ["notion-plan", "--target", str(owner)])
        self.assertEqual((plan["phase"], len(plan["updates"]), plan["findings"]), ("write", 2, []))
        page = Path(next(u["file"] for u in plan["updates"] if u["slug"] == "pricing")).read_text()
        self.assertTrue(page.startswith('<p color="gray">Read-only copy from the Acme pricing core'))
        for gone in ("type: concept", "# Pricing", "42 percent"):
            self.assertNotIn(gone, page)
        self.assertIn(f"We sell by [Tiers](https://www.notion.so/{ids['tiers']}).", page)
        self.assertIn("`[[not-a-link]]`", page)  # code is left alone
        root = Path(plan["root_update"]["file"]).read_text()
        self.assertIn("**Latest publish:**", root)
        self.assertIn(f"### Pricing marketing\n\n- [Pricing](https://www.notion.so/{ids['pricing']})", root)
        self.assertIn(f"- [Tiers](https://www.notion.so/{ids['tiers']}): the three tiers", root)
        self.assertNotIn("People with git", root)  # the fixture's remote is a local path
        self.assertEqual(root.strip().splitlines()[-1], f'<page url="https://www.notion.so/{"b" * 32}">All pages</page>')
        r = run(publish, ["notion-record", "--target", str(owner), "--synced", "all"])
        self.assertTrue(r["synced_at"])
        self.assertEqual(run(publish, ["notion-plan", "--target", str(owner)])["phase"], "done")
        self.assertEqual(run(publish, ["status", "--target", str(owner)])["notion"]["state"], "current")
        self.assertIn("publish-notion |", paths.log_path(owner).read_text())

    def test_only_changes_are_rewritten_and_removed_pages_listed(self):
        owner = self.published()
        self.linked(owner)
        run(publish, ["notion-plan", "--target", str(owner)])
        run(publish, ["notion-record", "--target", str(owner), "--synced", "all"])
        p = paths.wiki_dir(owner) / "concepts" / "tiers.md"
        p.write_text(p.read_text().replace("Three tiers.", "Four tiers."))
        self.assertEqual(run(publish, ["apply", "--target", str(owner), "--note", "four tiers"])["_exit"], 0)
        self.assertEqual(run(publish, ["status", "--target", str(owner)])["notion"]["state"], "behind the core repo")
        plan = run(publish, ["notion-plan", "--target", str(owner)])
        self.assertEqual([u["slug"] for u in plan["updates"]], ["tiers"])
        self.assertTrue(plan["root_update"])  # the publish log moved
        run(publish, ["notion-record", "--target", str(owner), "--synced", "all"])
        run(publish, ["mark", "tiers", "--visibility", "private", "--target", str(owner)])
        self.assertEqual(run(publish, ["apply", "--target", str(owner)])["_exit"], 0)
        plan = run(publish, ["notion-plan", "--target", str(owner)])
        self.assertEqual([d["slug"] for d in plan["deletes"]], ["tiers"])
        self.assertIn("pricing", [u["slug"] for u in plan["updates"]])  # its link to tiers became text
        r = run(publish, ["notion-record", "--target", str(owner), "--deleted", "tiers", "--synced", "all"])
        self.assertTrue(r["synced_at"])
        self.assertEqual(run(publish, ["notion-plan", "--target", str(owner)])["phase"], "done")

    def test_root_names_an_online_remote_and_dirty_core_is_refused(self):
        owner = self.published()
        self.linked(owner)
        cfg = publish.load_config(owner)
        cfg["remote"] = "git+https://github.com/acme/pricing-core.git"
        root = publish.render_notion_root(cfg, publish._core_snapshot(cfg), {}, self.ROOT,  # pylint: disable=protected-access
                                          publish.notion_url("b" * 32))
        self.assertIn("People with git read the same core from https://github.com/acme/pricing-core.git.", root)
        (self.root / "core" / "AGENTS.md").write_text("an agent's notes")  # outside context/: fine
        self.assertNotIn("error", run(publish, ["notion-plan", "--target", str(owner)]))
        (self.root / "core" / "context" / "wiki" / "concepts" / "stray.md").write_text("hand edit")
        r = run(publish, ["notion-plan", "--target", str(owner)])
        self.assertIn("uncommitted changes", r["error"])


if __name__ == "__main__":
    unittest.main()
