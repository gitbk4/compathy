"""Federation roundtrip: org layer -> team layer -> persona export -> search ->
import -> layered lint / query / discovery -> clone + sync -> update -> unlink.

Real git against local file:// remotes; no network. The org and team
fixtures are built once per class; each import test gets its own target.
"""
from __future__ import annotations

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

import compathy_query as cq  # noqa: E402
import discovery  # noqa: E402
import layers  # noqa: E402
import lineage as lin  # noqa: E402
import lint  # noqa: E402
import paths  # noqa: E402
import persona  # noqa: E402
import persona_integration  # noqa: E402
import scaffold  # noqa: E402


def git(repo: Path, *args):
    return subprocess.run(
        ["git", "-c", "user.email=lead@acme.com", "-c", "user.name=lead", *args],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )


def commit_all(repo: Path, msg: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def page(path: Path, ptype: str, title: str, body: str, **fm):
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = "".join(f"{k}: {v}\n" for k, v in fm.items())
    path.write_text(
        f"---\ntype: {ptype}\nschema_version: 1\ncreated: 2026-09-01\nupdated: 2026-09-01\n{extra}---\n"
        f"# {title}\n\n{body}\n", encoding="utf-8")


def add_index(target: Path, section: str, entry: str):
    idx = paths.index_path(target)
    # pylint: disable=protected-access
    idx.write_text(persona_integration._insert_index_entry(idx.read_text(), section=section, entry=entry))


def run(argv) -> dict:
    """Run persona.main and return its JSON output."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = persona.main(argv)
    out = buf.getvalue().strip()
    data = json.loads(out) if out else {}
    data["_exit"] = code
    return data


class FederationFixture(unittest.TestCase):
    """org (acme) + team (acme/payments) repos with personas, plus an isolated state home."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.root = Path(cls.td.name)
        cls.state = cls.root / "state"
        cls.fake_home = cls.root / "home"
        cls.fake_home.mkdir()
        cls._env = {k: os.environ.get(k) for k in (paths.STATE_HOME_ENV, "HOME")}
        os.environ[paths.STATE_HOME_ENV] = str(cls.state)
        os.environ["HOME"] = str(cls.fake_home)
        cls._pj = persona_integration.PERSONA_JSON_PATH
        persona_integration.PERSONA_JSON_PATH = cls.fake_home / "nope.json"

        # --- org layer
        cls.org = cls.root / "org"
        cls.org.mkdir()
        git(cls.org, "init", "-q", "-b", "main")
        scaffold.create_structure(cls.org, "acme")
        w = paths.wiki_dir(cls.org)
        page(w / "patterns" / "api-standards.md", "patterns", "API standards",
             "All services expose [[error-envelope]].", authority="org", override="narrow")
        page(w / "concepts" / "error-envelope.md", "concept", "Error envelope",
             "Every error is {code, message}.", authority="org", override="forbidden")
        page(w / "concepts" / "logging.md", "concept", "Logging", "Structured logs.",
             authority="org", override="free")
        page(w / "concepts" / "onboarding.md", "concept", "Onboarding", "Read [[api-standards]] first.")
        page(w / "patterns" / "technical-patterns.md", "patterns", "Technical patterns", "Org-wide: Go.")
        add_index(cls.org, "## Patterns", "- [[api-standards]] — how APIs look")
        add_index(cls.org, "## Patterns", "- [[technical-patterns]] — org defaults")
        add_index(cls.org, "## Concepts", "- [[error-envelope]] — the one error shape")
        add_index(cls.org, "## Concepts", "- [[logging]] — logs")
        add_index(cls.org, "## Concepts", "- [[onboarding]] — where to start")
        cls.org_source = "git+file://" + str(cls.org)
        assert run(["registry", "init", "--target", str(cls.org), "--org", "acme"])["_exit"] == 0
        cls.org_pin_wiki = commit_all(cls.org, "org wiki")  # what the team-lead persona pins
        spec = cls.root / "spec-org.json"
        spec.write_text(json.dumps({"title": "Team lead, acme", "summary": "Runs a team.", "tags": ["lead"],
                                    "reads_first": ["onboarding", "api-standards"],
                                    "responsibilities": ["keep the team linked"]}))
        r = run(["export", "write", "--target", str(cls.org), "--role", "team-lead", "--spec", str(spec),
                 "--layer-id", "acme", "--source", cls.org_source, "--exported-by", "lead@acme.com"])
        assert r["_exit"] == 0, r
        cls.org_pin = commit_all(cls.org, "export team-lead")
        run(["config", "set-org", cls.org_source])

        # --- team layer joins org
        cls.team = cls.root / "payments"
        cls.team.mkdir()
        git(cls.team, "init", "-q", "-b", "main")
        r = run(["import", "acme/team-lead", "--target", str(cls.team), "--apply", "--consent", "fetch,link",
                 "--as", "team", "--self-id", "acme/payments", "--project-name", "payments"])
        assert r["_exit"] == 0 and r["linked"] is True, r
        tw = paths.wiki_dir(cls.team)
        page(tw / "patterns" / "api-standards.md", "patterns", "API standards (payments)",
             "Idempotency keys required. See [[onboarding]].", extends="acme")
        page(tw / "patterns" / "technical-patterns.md", "patterns", "Technical patterns", "Go, Postgres, Temporal.")
        page(tw / "concepts" / "ledger-model.md", "concept", "Ledger model",
             "Double-entry. See [[api-standards]].", authority="team")
        add_index(cls.team, "## Patterns", "- [[api-standards]] — payments narrowing")
        add_index(cls.team, "## Patterns", "- [[technical-patterns]] — how we code")
        add_index(cls.team, "## Concepts", "- [[ledger-model]] — the ledger")
        commit_all(cls.team, "team wiki")
        cls.team_source = "git+file://" + str(cls.team)
        spec = cls.root / "spec-team.json"
        spec.write_text(json.dumps({
            "title": "Backend engineer, Payments", "summary": "Owns ledger + settlement.",
            "tags": ["backend", "go", "payments"],
            "reads_first": ["acme/api-standards", "api-standards", "ledger-model"],
            "responsibilities": ["settlement pipeline"],
            "toolkit": {"claude_skills": [{"name": "compathy", "github": "gitbk4/compathy"}],
                        "mcp_servers": [{"id": "pg-readonly", "command": "python3", "args": ["-m", "pg_mcp"],
                                         "description": "read-only analytics"}]}}))
        r = run(["export", "write", "--target", str(cls.team), "--role", "backend-engineer", "--spec", str(spec),
                 "--source", cls.team_source])
        assert r["_exit"] == 0, r
        cls.team_manifest = r["persona"]
        cls.team_pin = commit_all(cls.team, "export backend-engineer")
        r = run(["registry", "add-team", "--target", str(cls.org), "--id", "payments", "--source", cls.team_source])
        assert r["_exit"] == 0, r
        cls.org_pin2 = commit_all(cls.org, "register payments")

    @classmethod
    def tearDownClass(cls):
        persona_integration.PERSONA_JSON_PATH = cls._pj
        for k, v in cls._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        layers.remove_tree(cls.state)
        cls.td.cleanup()

    def new_target(self, name="svc") -> Path:
        t = Path(tempfile.mkdtemp(dir=str(self.root))) / name
        t.mkdir()
        git(t, "init", "-q", "-b", "main")
        self.addCleanup(lambda: shutil.rmtree(t.parent, ignore_errors=True))
        return t

    def joined(self, name="svc", consent="fetch,link,toolkit") -> Path:
        t = self.new_target(name)
        r = run(["import", "acme/payments/backend-engineer", "--target", str(t), "--apply",
                 "--consent", consent, "--project-name", name])
        self.assertEqual(r["_exit"], 0, r)
        return t


class TestExport(FederationFixture):
    def test_manifest_shape_and_pins(self):
        m = self.team_manifest
        self.assertEqual(lin.validate_manifest(m), [])
        self.assertEqual([l["id"] for l in m["layers"]], ["acme/payments", "acme"])
        # The org persona was exported at the "org wiki" commit; the export commit came after.
        self.assertEqual(m["layers"][1]["pin"], self.org_pin_wiki)
        self.assertEqual(m["layers"][0]["pin"], git(self.team, "rev-parse", "HEAD~1").stdout.strip())
        self.assertEqual(m["reads_first"],
                         ["acme/api-standards", "acme/payments/api-standards", "acme/payments/ledger-model"])
        self.assertEqual(m["policy"]["read_only"], ["acme"])
        idx = lin.read_json(paths.personas_index_path(self.team))
        self.assertEqual([p["id"] for p in idx["personas"]], ["acme/payments/backend-engineer"])
        self.assertEqual(lint.lint(self.team)["summary"]["errors"], 0)

    def test_propose_lists_candidates(self):
        r = run(["export", "propose", "--target", str(self.team), "--role", "sre", "--source", self.team_source])
        self.assertEqual(r["_exit"], 0, r)
        self.assertEqual(r["persona_id"], "acme/payments/sre")
        self.assertIn("ledger-model", r["candidates"]["authoritative"])
        self.assertIn("backend-engineer", r["existing_personas"])
        self.assertTrue(r["preconditions"]["git_clean"])

    def test_refuses_dirty_and_missing_refs(self):
        (paths.wiki_dir(self.team) / "index.md").write_text(
            (paths.wiki_dir(self.team) / "index.md").read_text() + "\n")
        try:
            spec = self.root / "spec-x.json"
            spec.write_text(json.dumps({"title": "X", "reads_first": ["ledger-model"]}))
            r = run(["export", "write", "--target", str(self.team), "--role", "x", "--spec", str(spec),
                     "--source", self.team_source])
            self.assertEqual(r["_exit"], 1)
            self.assertIn("uncommitted", r["error"])
        finally:
            git(self.team, "checkout", "--", "context/wiki/index.md")
        spec.write_text(json.dumps({"title": "X", "reads_first": ["does-not-exist"]}))
        r = run(["export", "write", "--target", str(self.team), "--role", "x", "--spec", str(spec),
                 "--source", self.team_source])
        self.assertEqual(r["_exit"], 1)
        self.assertIn("does-not-exist", r["error"])
        self.assertFalse((paths.personas_dir(self.team) / "x.json").exists())

    def test_root_layer_needs_layer_id(self):
        r = run(["export", "write", "--target", str(self.org), "--role", "y", "--title", "Y", "--source", self.org_source])
        self.assertEqual(r["_exit"], 1)
        self.assertIn("--layer-id", r["error"])


class TestLayeredLint(FederationFixture):
    def test_team_lint_clean_with_upward_links(self):
        res = lint.lint(self.team)
        self.assertEqual(res["summary"], {"errors": 0, "warnings": 0}, res)
        self.assertEqual(res["lineage"]["persona"], "acme/team-lead")
        self.assertEqual(res["lineage"]["self"], {"id": "acme/payments", "role": "team"})

    def test_shadow_matrix_and_authority_claim(self):
        tw = paths.wiki_dir(self.team)
        try:
            page(tw / "concepts" / "error-envelope.md", "concept", "EE", "trace id")          # forbidden
            page(tw / "concepts" / "logging.md", "concept", "Logging (payments)", "json")       # free
            page(tw / "concepts" / "bad-claim.md", "concept", "Bad", "x", authority="org")     # claim
            page(tw / "patterns" / "api-standards.md", "patterns", "API standards (payments)",
                 "no extends")                                                               # narrow w/o extends
            res = lint.lint(self.team)
            kinds = sorted((e["kind"], e.get("slug")) for e in res["errors"])
            self.assertEqual(kinds, [("authority-claim", "bad-claim"),
                                     ("shadow-forbidden", "error-envelope"),
                                     ("shadow-missing-extends", "api-standards")])
            # layer-local collision (technical-patterns) and free override produce nothing
            policy_kinds = ("shadow-forbidden", "shadow-missing-extends", "authority-claim")
            self.assertFalse(any(i.get("slug") in ("technical-patterns", "logging")
                                 for i in res["errors"] + res["warnings"] if i["kind"] in policy_kinds))
        finally:
            git(self.team, "checkout", "--", "context")
            git(self.team, "clean", "-fdq", "context")

    def test_extends_unknown_layer_warns(self):
        tw = paths.wiki_dir(self.team)
        try:
            page(tw / "concepts" / "spare.md", "concept", "Spare", "x", extends="zzz")
            add_index(self.team, "## Concepts", "- [[spare]] — spare")
            res = lint.lint(self.team)
            self.assertIn("extends-unknown-layer", {w["kind"] for w in res["warnings"]})
        finally:
            git(self.team, "checkout", "--", "context")
            git(self.team, "clean", "-fdq", "context")

    def test_invalid_field_values(self):
        tw = paths.wiki_dir(self.team)
        try:
            page(tw / "concepts" / "weird.md", "concept", "W", "x", authority="galaxy", override="maybe")
            page(tw / "concepts" / "weird2.md", "concept", "W2", "x", override="free")
            add_index(self.team, "## Concepts", "- [[weird]] — w")
            add_index(self.team, "## Concepts", "- [[weird2]] — w2")
            res = lint.lint(self.team)
            self.assertEqual({e["kind"] for e in res["errors"]}, {"invalid-authority", "invalid-override"})
            self.assertIn("override-without-authority", {w["kind"] for w in res["warnings"]})
        finally:
            git(self.team, "checkout", "--", "context")
            git(self.team, "clean", "-fdq", "context")

    def test_manifest_and_registry_validation(self):
        bad = paths.personas_dir(self.team) / "broken.json"
        try:
            bad.write_text('{"kind": "nope"}')
            res = lint.lint(self.team)
            self.assertIn("persona-manifest-invalid", {e["kind"] for e in res["errors"]})
        finally:
            bad.unlink()
        rp = paths.registry_path(self.org)
        orig = rp.read_text()
        try:
            rp.write_text('{"schema_version": 1, "org": "acme", "teams": [{"id": "a/b"}]}')
            res = lint.lint(self.org)
            self.assertIn("registry-invalid", {e["kind"] for e in res["errors"]})
        finally:
            rp.write_text(orig)

    def test_standalone_output_unchanged(self):
        t = self.new_target("plain")
        scaffold.create_structure(t, "plain")
        res = lint.lint(t)
        self.assertEqual(set(res.keys()), {"errors", "warnings", "summary"})
        self.assertEqual(res["summary"], {"errors": 0, "warnings": 0})


class TestSearch(FederationFixture):
    def test_registry_search_ranks_and_trusts(self):
        r = run(["search", "payments backend", "--refresh"])
        self.assertEqual(r["_exit"], 0, r)
        ids = [x["id"] for x in r["results"]]
        self.assertEqual(ids[0], "acme/payments/backend-engineer")
        self.assertTrue(all(x["trust"] == 4 for x in r["results"]))
        self.assertEqual(r["org"]["id"], "acme")
        self.assertTrue(r["results"][0]["why"])

    def test_empty_query_lists_all(self):
        r = run(["search"])
        self.assertEqual({x["id"] for x in r["results"]}, {"acme/team-lead", "acme/payments/backend-engineer"})

    def test_unreachable_team_soft_fails(self):
        rp = paths.registry_path(self.org)
        data = lin.read_json(rp)
        data["teams"].append({"id": "ghost", "source": "git+file:///nonexistent/ghost", "path": "context"})
        rp.write_text(json.dumps(data))
        pin = commit_all(self.org, "ghost team")
        try:
            r = run(["search", "backend", "--refresh"])
            self.assertEqual(r["_exit"], 0)
            self.assertTrue(any("ghost" in w for w in r["warnings"]))
            self.assertTrue(r["results"])
        finally:
            git(self.org, "revert", "--no-edit", pin)
            run(["search", "--refresh"])  # re-prime the cache without the ghost

    def test_local_search_needs_no_org(self):
        r = run(["search", "lead", "--local", "--target", str(self.team)])
        self.assertEqual(r["_exit"], 0, r)
        self.assertTrue(r["local_only"])
        self.assertIn("acme/team-lead", [x["id"] for x in r["results"]])


class TestImport(FederationFixture):
    def test_plan_has_no_side_effects(self):
        t = self.new_target("plan-only")
        r = run(["import", "acme/payments/backend-engineer", "--target", str(t)])
        self.assertEqual(r["_exit"], 0, r)
        self.assertEqual(r["mode"], "plan")
        self.assertEqual(r["source_kind"], "registry")
        self.assertEqual(r["trust"], 4)
        self.assertTrue(r["scaffold_needed"])
        self.assertEqual([l["id"] for l in r["layers"]], ["acme/payments", "acme"])
        self.assertFalse((t / "context").exists())
        self.assertTrue(any("unsigned" in w for w in r["warnings"]))

    def test_apply_links_everything(self):
        t = self.joined("svc")
        doc = lin.load_lineage(t)
        self.assertEqual(doc["persona"], "acme/payments/backend-engineer")
        self.assertEqual(doc["self"], {"id": "svc", "role": "project"})
        # Export pins the HEAD it ran at (the persona file itself is committed after).
        self.assertEqual([l["pin"] for l in doc["layers"]], [l["pin"] for l in self.team_manifest["layers"]])
        self.assertEqual(doc["layers"][1]["pin"], self.org_pin_wiki)
        self.assertEqual(lin.read_json(paths.persona_path(t))["id"], "acme/payments/backend-engineer")
        ent = paths.wiki_dir(t) / "entities" / "persona-backend-engineer.md"
        self.assertTrue(ent.is_file())
        self.assertEqual(ent.read_text().count("[[api-standards]]"), 1, "same slug in two layers links once")
        self.assertIn("[[persona-backend-engineer]]", paths.index_path(t).read_text())
        claude = (t / "CLAUDE.md").read_text()
        self.assertIn("### Federation", claude)
        self.assertIn("Backend engineer, Payments", claude)
        self.assertIn("1. `acme` (org", claude)
        self.assertIn("2. `acme/payments` (team", claude)
        self.assertNotIn("—", claude.split("### Federation")[1])
        mcp = json.loads((t / ".mcp.json").read_text())
        self.assertEqual(set(mcp["mcpServers"]), {"compathy-wiki", "pg-readonly"})
        self.assertNotIn("--layer", " ".join(mcp["mcpServers"]["compathy-wiki"]["args"]))
        self.assertTrue((paths.personas_home_dir() / "acme--payments--backend-engineer.json").is_file())
        log_lines = paths.import_log_path().read_text().splitlines()
        self.assertTrue(any("acme/payments/backend-engineer" in l for l in log_lines))
        self.assertIn("import | persona acme/payments/backend-engineer (trust 4)", paths.log_path(t).read_text())
        res = lint.lint(t)
        self.assertEqual(res["summary"], {"errors": 0, "warnings": 0}, res)

    def test_reimport_is_noop_and_consent_is_required(self):
        t = self.joined("twice")
        before = paths.lineage_path(t).read_text()
        r = run(["import", "acme/payments/backend-engineer", "--target", str(t), "--apply", "--consent", "fetch,link"])
        self.assertEqual(r["linked"], "already")
        self.assertTrue(r["already_linked_at_these_pins"])
        self.assertEqual(paths.lineage_path(t).read_text(), before)
        r = run(["import", "acme/payments/backend-engineer", "--target", str(self.new_target("noc")), "--apply"])
        self.assertEqual(r["_exit"], 1)
        self.assertIn("consent", r["error"])
        r = run(["import", "acme/payments/backend-engineer", "--target", str(self.new_target("bad")), "--apply",
                 "--consent", "fetch,rootkit"])
        self.assertEqual(r["_exit"], 1)

    def test_fetch_only_consent_writes_nothing_to_project(self):
        t = self.new_target("fetchonly")
        r = run(["import", "acme/payments/backend-engineer", "--target", str(t), "--apply", "--consent", "fetch"])
        self.assertEqual(r["_exit"], 0, r)
        self.assertFalse(r["linked"])
        self.assertFalse((t / "context").exists())

    def test_file_import_trust_and_registry_match(self):
        t = self.new_target("fromfile")
        f = self.root / "handed.json"
        f.write_text(json.dumps(self.team_manifest))
        r = run(["import", str(f), "--target", str(t)])
        self.assertEqual(r["source_kind"], "file")
        self.assertTrue(r["registry_match"])
        self.assertEqual(r["trust"], 4)
        tampered = dict(self.team_manifest, summary="changed out of band")
        f.write_text(json.dumps(tampered))
        r = run(["import", str(f), "--target", str(t)])
        self.assertFalse(r["registry_match"])
        self.assertEqual(r["trust"], 2)

    def test_tampered_tree_sha_aborts_before_link(self):
        t = self.new_target("tamper")
        f = self.root / "tampered.json"
        bad = json.loads(json.dumps(self.team_manifest))
        bad["layers"][1]["tree_sha"] = "0" * 40
        f.write_text(json.dumps(bad))
        layers.remove_tree(lin.layer_cache_root(bad["layers"][1]))
        r = run(["import", str(f), "--target", str(t), "--apply", "--consent", "fetch,link"])
        self.assertEqual(r["_exit"], 1)
        self.assertIn("nothing linked", r["error"])
        self.assertFalse((t / "context").exists())
        # restore the good org cache for the other tests
        layers.fetch_layer(self.team_manifest["layers"][1])

    def test_url_import(self):
        t = self.new_target("fromurl")
        payload = json.dumps(self.team_manifest).encode()

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(persona.urllib.request, "urlopen", return_value=_Resp(payload)):
            r = run(["import", "https://example.com/p.json", "--target", str(t), "--org", self.org_source])
        self.assertEqual(r["source_kind"], "url")
        self.assertEqual(r["trust"], 4)  # byte-identical to registry copy
        r = run(["import", "http://example.com/p.json", "--target", str(t)])
        self.assertEqual(r["_exit"], 1)

    def test_unknown_id_and_missing_org(self):
        t = self.new_target("unknown")
        r = run(["import", "acme/nosuchteam/x", "--target", str(t)])
        self.assertEqual(r["_exit"], 1)
        self.assertIn("not listed", r["error"])
        cfg = paths.config_path()
        saved = cfg.read_text()
        try:
            cfg.unlink()
            r = run(["import", "acme/payments/backend-engineer", "--target", str(t)])
            self.assertEqual(r["_exit"], 1)
            self.assertIn("--org", r["error"])
        finally:
            cfg.write_text(saved)

    def test_scaffold_from_persona(self):
        t = self.new_target("viascaffold")
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(sys, "argv", ["scaffold.py", "--target", str(t),
                                                                   "--from-persona", "acme/payments/backend-engineer"]):
            code = scaffold.main()
        self.assertEqual(code, 0, buf.getvalue()[-500:])
        self.assertTrue(paths.lineage_path(t).is_file())
        self.assertFalse((t / ".mcp.json").read_text().count("pg-readonly"), "toolkit not consented by default")


class TestQueryAcrossLayers(FederationFixture):
    def test_get_page_resolution(self):
        t = self.joined("q1")
        out = cq.call_tool("compathy_get_page", t, {"slug": "api-standards"})
        self.assertEqual(out["layer"], "acme/payments")
        self.assertEqual([a["layer"] for a in out["also_in"]], ["acme"])
        out = cq.call_tool("compathy_get_page", t, {"slug": "api-standards", "layer": "acme"})
        self.assertEqual(out["layer"], "acme")
        self.assertEqual(out["also_in"][0]["layer"], "acme/payments")
        out = cq.call_tool("compathy_get_page", t, {"slug": "error-envelope", "include_neighbors": True})
        self.assertEqual(out["layer"], "acme")
        self.assertEqual([n["slug"] for n in out["neighbors"]["inbound"]], ["api-standards"])
        local = cq.call_tool("compathy_get_page", t, {"slug": "persona-backend-engineer"})
        self.assertEqual(local["layer"], "q1")
        self.assertEqual(local["also_in"], [])
        with self.assertRaises(cq.ToolError):
            cq.call_tool("compathy_get_page", t, {"slug": "ledger-model", "layer": "acme"})
        with self.assertRaises(cq.ToolError):
            cq.call_tool("compathy_get_page", t, {"slug": "x", "layer": "nope"})

    def test_search_list_index_layers_personas(self):
        t = self.joined("q2")
        s = cq.call_tool("compathy_search", t, {"query": "idempotency"})
        self.assertEqual([(r["slug"], r["layer"]) for r in s["results"]], [("api-standards", "acme/payments")])
        s = cq.call_tool("compathy_search", t, {"query": "api standards", "layer": "acme"})
        self.assertTrue(all(r["layer"] == "acme" for r in s["results"]))
        lp = cq.call_tool("compathy_list_pages", t, {"page_type": "patterns"})
        self.assertEqual([(p["slug"], p["layer"]) for p in lp["pages"]],
                         [("api-standards", "acme/payments"), ("technical-patterns", "acme/payments"),
                          ("api-standards", "acme"), ("technical-patterns", "acme")])
        idx = cq.call_tool("compathy_index", t, {})
        self.assertEqual([l["layer"] for l in idx["layers"]], ["q2", "acme/payments", "acme"])
        ly = cq.call_tool("compathy_layers", t, {})
        self.assertTrue(ly["linked"])
        self.assertEqual(ly["persona"], "acme/payments/backend-engineer")
        self.assertEqual(ly["warnings"], [])
        ps = cq.call_tool("compathy_personas_search", t, {"query": "backend"})
        self.assertEqual(ps["results"][0]["id"], "acme/payments/backend-engineer")
        names = {tl["name"] for tl in cq.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, t)["result"]["tools"]}
        self.assertIn("compathy_layers", names)

    def test_standalone_query_shape(self):
        t = self.new_target("q3")
        scaffold.create_structure(t, "q3")
        idx = cq.call_tool("compathy_index", t, {})
        self.assertNotIn("layers", idx)
        ly = cq.call_tool("compathy_layers", t, {})
        self.assertFalse(ly["linked"])
        self.assertEqual(ly["depth"], 1)


class TestLifecycle(FederationFixture):
    def test_clone_then_sync_rehydrates(self):
        t = self.joined("origin-proj")
        commit_all(t, "linked")
        clone = t.parent / "clone"
        git(t.parent, "clone", "-q", str(t), str(clone))
        # Wipe the cache to simulate a teammate's machine.
        layers.remove_tree(self.state / "layers")
        try:
            who = run(["whoami", "--target", str(clone)])
            self.assertTrue(who["linked"])
            self.assertTrue(who["needs_sync"])
            res = lint.lint(clone)
            self.assertEqual(res["summary"]["errors"], 0)
            self.assertIn("layer-not-cached", {w["kind"] for w in res["warnings"]})
            self.assertIn("unverified-backlink", {w["kind"] for w in res["warnings"]})
            r = run(["sync", "--target", str(clone)])
            self.assertTrue(r["ok"], r)
            self.assertEqual([l["action"] for l in r["layers"]], ["fetched", "fetched"])
            self.assertEqual(lint.lint(clone)["summary"], {"errors": 0, "warnings": 0})
            self.assertFalse(run(["whoami", "--target", str(clone)])["needs_sync"])
        finally:
            for l in self.team_manifest["layers"]:
                layers.fetch_layer(l)

    def test_status_update_unlink(self):
        t = self.joined("life")
        st = run(["status", "--target", str(t), "--check-upstream"])
        self.assertTrue(st["all_verified"])
        by = {l["id"]: l for l in st["layers"]}
        self.assertEqual(by["acme"]["upstream"], "differs")  # org gained the registry commit after the pin
        # update plan shows a diff, does not write
        before = paths.lineage_path(t).read_text()
        up = run(["update", "--target", str(t), "--layer", "acme"])
        self.assertFalse(up["applied"])
        ch = up["changes"][0]
        self.assertEqual(ch["status"], "changed")
        expected_new = layers.upstream_head(self.org_source)
        self.assertEqual(ch["new"], expected_new)
        self.assertIn("index.md", ch["index_diff"])
        self.assertEqual(paths.lineage_path(t).read_text(), before)
        up = run(["update", "--target", str(t), "--layer", "acme", "--apply"])
        self.assertTrue(up["applied"])
        doc = lin.load_lineage(t)
        self.assertEqual({l["id"]: l["pin"] for l in doc["layers"]}["acme"], expected_new)
        self.assertIn(expected_new[:12], (t / "CLAUDE.md").read_text())
        self.assertEqual(run(["status", "--target", str(t), "--check-upstream"])["layers"][1]["upstream"], "current")
        r = run(["update", "--target", str(t), "--to", "1" * 40])
        self.assertEqual(r["_exit"], 1)
        # unlink removes link files + seeded page, wiki stays lint-clean
        un = run(["unlink", "--target", str(t)])
        self.assertEqual(un["was"], "acme/payments/backend-engineer")
        self.assertFalse(paths.lineage_path(t).exists())
        self.assertFalse((paths.wiki_dir(t) / "entities" / "persona-backend-engineer.md").exists())
        self.assertNotIn("Federation", (t / "CLAUDE.md").read_text())
        self.assertEqual(lint.lint(t)["summary"], {"errors": 0, "warnings": 0})
        self.assertEqual(run(["status", "--target", str(t)])["_exit"], 1)

    def test_resolve_advice(self):
        t = self.joined("adv")
        r = run(["resolve", "error-envelope", "--target", str(t)])
        self.assertIn("may not be overridden", r["advice"])
        r = run(["resolve", "api-standards", "--target", str(t)])
        self.assertEqual(r["found_in"][0]["layer"], "acme/payments")
        self.assertIn("link to it", r["advice"])
        r = run(["resolve", "brand-new", "--target", str(t)])
        self.assertEqual(r["advice"], "free to create")
        r = run(["resolve", "persona-backend-engineer", "--target", str(t)])
        self.assertEqual(r["advice"], "exists locally; edit it")


class TestDiscoveryRendering(unittest.TestCase):
    def test_standalone_unchanged_and_linked_block(self):
        plain = discovery.render_context_section("demo")
        self.assertNotIn("Federation", plain)
        doc = {"schema_version": 1, "persona": "acme/payments/backend-engineer",
               "layers": [{"id": "acme/payments", "role": "team", "path": "context", "pin": "1" * 40},
                          {"id": "acme", "role": "org", "path": "context", "pin": "2" * 40}]}
        manifest = {"id": "acme/payments/backend-engineer", "title": "Backend engineer", "summary": "Owns ledger.",
                    "reads_first": ["acme/api-standards", "acme/payments/ledger-model"],
                    "responsibilities": ["settlement"]}
        linked = discovery.render_context_section("demo", doc, manifest)
        self.assertTrue(linked.startswith(plain.split(discovery.SENTINEL_END)[0]))
        self.assertIn("1. `acme` (org, pin `222222222222`)", linked)
        self.assertIn("2. `acme/payments` (team", linked)
        self.assertIn("3. this project", linked)
        self.assertIn("`api-standards` (in `acme`)", linked)
        self.assertIn("Responsibilities: settlement.", linked)
        self.assertNotIn("—", linked)
        self.assertTrue(linked.endswith(discovery.SENTINEL_END))


if __name__ == "__main__":
    unittest.main()
