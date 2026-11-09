#!/usr/bin/env python3
"""Bookkeeping and guardrails for /compathy-respond (the Slack stand-in).

The model does the reading, the judgement and the connector calls. This
script does everything that must not depend on the model: which pages may
be read, which links may be shared, whether a drafted reply may be posted,
and the record of what happened. Stdlib only.

Subcommands (all print JSON; exit 0 ok, 1 refusal, 2 usage):

  init      scaffold context/respond/ from templates and seed the registry
  corpus    the allowed pages after the policy filters
  search    filtered search over the corpus (wraps compathy_query)
  page      an allowed page's body, its links classified against the registry
  sources   registry entries, optionally matched
  window    start | status | stop | update
  tick      begin | end
  check     validate a drafted reply BEFORE it is posted
  record    append an interaction to the ledger (re-runs the check)
  review    list | grade | reaction | stats | export
  escalate  send an escalation through a route (imessage, twilio, dry)
  export    build a kit another agent (a ChatGPT workspace agent) runs the stand-in from

Usage:
  python3 scripts/respond.py --target /path/to/project <subcommand> [args]
  python3 scripts/respond.py --target . --layer ../other-project corpus
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# pylint: disable=wrong-import-position
import compathy_query as cq  # noqa: E402
from lint import _parse_value, parse_frontmatter  # noqa: E402
from paths import (  # noqa: E402
    INDEX_FILE,
    LOG_FILE,
    RESPOND_STATE_FILE,
    WIKI_SUBDIRS,
    context_root,
    respond_dir,
    respond_ledger_path,
    respond_lessons_path,
    respond_policy_path,
    respond_sources_path,
    respond_state_path,
    schema_path,
    wiki_dir,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
OVERLAP_SECONDS = 600
HANDLED_RETENTION_DAYS = 30
POSTED_OUTCOMES = ("answered", "clarified", "escalated")
ALL_OUTCOMES = POSTED_OUTCOMES + ("ignored", "handed-over")
CONFIDENCES = ("high", "medium", "low")
REGISTRY_COLUMNS = ("title", "url", "kind", "audience", "status", "verified", "via", "note")
MANUAL_VIA = "manual"
DRIVE_KINDS = ("gdoc", "gsheet", "gslides")
MANUAL_KINDS = ("claude-artifact", "claude-design", "figma", "sharepoint", "other")
DRIVE_ID_RE = re.compile(r"/(?:d|folders)/([A-Za-z0-9_-]{5,})")
NOTION_ID_RE = re.compile(r"([0-9a-f]{32})(?:[?#]|$)", re.I)
REGISTRY_STATUSES = ("shared", "private", "unknown")
LESSON_SECTIONS = ("House answers", "Do not answer", "Clarify first", "Phrasing", "Gaps")

URL_RE = re.compile(r"https?://[^\s<>|)\]\"']+")
INDEX_BULLET_RE = re.compile(
    r"^\s*-\s*\[\[([^\]|]+)(?:\|[^\]]*)?\]\]\s*[:\-—–]*\s*(.*)$"
)
ENTRY_RE = re.compile(
    r"^## \[(?P<ts>[^\]]+)\] (?P<id>R-\d+) \| (?P<outcome>[\w-]+) \| "
    r"(?P<intent>[^|\n]*?) \| (?P<confidence>[^|\n]*)$",
    re.M,
)
PERSON_TITLE_RE = re.compile(r"^[A-Z][a-z]+(?: [A-Z][a-z]+){1,2}$")
DROP_QUERY_KEYS = {"authuser", "usp", "tab", "hl", "gid"}

DEFAULT_POLICY = {
    "type": "respond-policy",
    "schema_version": 1,
    "owner_name": "the owner",
    "owner_slack_id": "",
    "internal_domain": "",
    "triggers": ["mentions", "dms", "group-dm-mentions"],
    "deny_channels": [],
    "test_channels": [],
    "allow_types": ["concept", "entity", "patterns", "summary"],
    "deny_source_patterns": [
        "slack", "grain", "thread", "notes-", "1on1", "1-1", "email", "dm-", "transcript",
    ],
    "deny_slugs": [],
    "allow_slugs": [],
    "intents": ["source-document", "build-status", "availability", "roadmap", "feature"],
    "roadmap_page": "",
    "link_audiences": ["internal", "all"],
    "max_posts_per_tick": 5,
    "max_clarifications_per_thread": 1,
    "tighten_after_negatives": 2,
    "max_reply_lines": 14,
    "max_reply_chars": 3000,
    "never_say": [],
    "forbidden_phrases": [
        "i approve", "i'll approve", "i will approve", "i promise", "i've decided",
        "i decided", "i commit", "we commit", "consider it approved",
        "you have my approval", "i'll sign off", "i sign off",
    ],
    "link_verify_max_age_days": 7,
    "kind_rules": ["public=shared/all", "linear=shared/internal", "storybook=shared/internal"],
    "notion_default": "shared",
    "escalation_routes": ["push"],
    "escalation_channel": "",
    "imessage_to": "",
    "header": ":robot_face: *Automated reply from the owner's stand-in (this is not the owner).*",
    "footer": "_Useful? React :+1: or :-1: so the owner can tune me._",
    "working_hours": "",
}


class Refusal(Exception):
    """A deterministic refusal; the CLI prints it as JSON and exits 1."""


# ---------- small helpers ----------

def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_when(s: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError as e:
        raise Refusal(f"not an ISO date-time: {s!r} ({e})") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_now().tzinfo)
    return dt


def _parse_every(s: str) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", str(s))
    if not m:
        raise Refusal(f"not a duration like 5m, 1h, 7d: {s!r}")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _as_list(v) -> list:
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


def _csv(s) -> list:
    if not s:
        return []
    return [p.strip() for p in str(s).split(",") if p.strip()]


def _clean_url(u: str) -> str:
    """A URL as found in prose, without the trailing punctuation the regex swallows."""
    return str(u).strip().rstrip(".,;:)>'\"")


def normalize_url(u: str) -> str:
    """Canonical form for comparing links: no fragment, no view params, no trailing punctuation."""
    u = _clean_url(u)
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return u.lower()
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if k.lower() not in DROP_QUERY_KEYS]
    q.sort()
    path = p.path.rstrip("/")
    return urllib.parse.urlunsplit(
        (p.scheme.lower(), p.netloc.lower(), path, urllib.parse.urlencode(q), "")
    )


def _kind_for(url: str) -> str:
    u = url.lower()
    if "docs.google.com/document" in u:
        return "gdoc"
    if "docs.google.com/spreadsheets" in u:
        return "gsheet"
    if "docs.google.com/presentation" in u:
        return "gslides"
    if "claude.ai/artifact" in u:
        return "claude-artifact"
    if "claude.ai/design" in u:
        return "claude-design"
    if "linear.app" in u:
        return "linear"
    if "notion" in u:
        return "notion"
    if "storybook" in u:
        return "storybook"
    if "figma.com" in u:
        return "figma"
    if "sharepoint.com" in u or "onedrive" in u:
        return "sharepoint"
    try:
        host = urllib.parse.urlsplit(u).netloc.lower()
    except ValueError:
        host = ""
    if host.endswith((".gov", ".edu")) or host.endswith(".gov.uk"):
        return "public"
    return "other"  # unknown host: never auto-shared; a manual row or a connector verdict decides


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _write_atomic(p: Path, text: str) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def _render(name: str, ctx: dict) -> str:
    tpl = TEMPLATES_DIR / f"{name}.tmpl"
    content = _read(tpl)
    for k, v in ctx.items():
        content = content.replace("{{" + k + "}}", str(v))
    return content


# ---------- policy, registry, lessons ----------

def _require_respond(target: Path) -> Path:
    rd = respond_dir(target)
    if not rd.is_dir():
        raise Refusal(f"no {rd} (run `respond.py init` first)")
    return rd


def load_policy(target: Path) -> tuple:
    """Return (policy dict merged over defaults, body text)."""
    p = respond_policy_path(target)
    if not p.is_file():
        raise Refusal(f"missing {p}")
    try:
        fm, body = parse_frontmatter(_read(p))
    except ValueError as e:
        raise Refusal(f"policy.md frontmatter: {e}") from e
    policy = dict(DEFAULT_POLICY)
    for k, v in fm.items():
        policy[k] = v
    for k in ("triggers", "deny_channels", "test_channels", "allow_types", "deny_source_patterns",
              "deny_slugs", "allow_slugs", "intents", "link_audiences", "never_say",
              "forbidden_phrases", "escalation_routes", "kind_rules"):
        policy[k] = _as_list(policy.get(k))
    return policy, body


def parse_table(body: str) -> list:
    """Parse the first markdown table in ``body`` into a list of dicts (lowercased headers)."""
    rows = []
    header = None
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            if header is not None and rows:
                break
            continue
        cells = [c.strip().replace("\u0000", "|")
                 for c in line.replace("\\|", "\u0000").strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if all(re.fullmatch(r":?-{2,}:?", c or "--") for c in cells):
            continue
        row = {}
        for i, h in enumerate(header):
            row[h] = cells[i] if i < len(cells) else ""
        rows.append(row)
    return rows


def load_registry(target: Path) -> tuple:
    """Return (entries, body). Each entry has the table's columns plus ``url_norm``."""
    p = respond_sources_path(target)
    if not p.is_file():
        raise Refusal(f"missing {p}")
    fm, body = parse_frontmatter(_read(p))
    if fm.get("type") not in (None, "respond-registry"):
        raise Refusal("sources.md: frontmatter type must be respond-registry")
    entries = []
    for row in parse_table(body):
        if not row.get("url"):
            continue
        row["url_norm"] = normalize_url(row["url"])
        row["status"] = (row.get("status") or "unknown").lower()
        row["audience"] = [a.strip().lower() for a in (row.get("audience") or "internal").split(",") if a.strip()]
        row["via"] = (row.get("via") or "").strip()
        row["kind"] = (row.get("kind") or _kind_for(row["url"])).strip().lower()
        entries.append(row)
    return entries, body


def _verified_age_days(entry: dict):
    """Days since the row's Verified date, or None when it has none or it is unparsable."""
    v = (entry.get("verified") or "").strip()
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v[:10]).date()
    except ValueError:
        return None
    return (_now().date() - d).days


def is_stale(entry: dict, policy: dict) -> bool:
    """A shared row counts as stale past ``link_verify_max_age_days``; manual rows never do."""
    if entry.get("via") == MANUAL_VIA:
        return False
    age = _verified_age_days(entry)
    return age is None or age > int(policy["link_verify_max_age_days"])


def link_ids(url: str) -> dict:
    """The connector ids a verifier needs: a Drive file id or a Notion page id."""
    out = {}
    m = DRIVE_ID_RE.search(url)
    if m and "google.com" in url:
        out["drive_file_id"] = m.group(1)
    m = NOTION_ID_RE.search(url.replace("-", "") if "notion" in url else "")
    if m:
        out["notion_page_id"] = m.group(1).lower()
    return out


def parse_kind_rules(policy: dict) -> dict:
    """``kind=status/audience`` strings -> {kind: (status, audience)}."""
    rules = {}
    for item in policy.get("kind_rules", []):
        if "=" not in item:
            continue
        kind, _, rest = item.partition("=")
        status, _, audience = rest.partition("/")
        rules[kind.strip().lower()] = (status.strip().lower() or "unknown", audience.strip().lower() or "internal")
    return rules


def drive_verdict(permissions: list, internal_domain: str) -> dict:
    """Status and audience from a Drive permissions list (any role counts as view)."""
    domain = (internal_domain or "").lower()
    types = {str(p.get("type", "")).lower(): p for p in permissions if isinstance(p, dict)}
    if "anyone" in types:
        return {"status": "shared", "audience": "all", "via": "drive:anyone"}
    for p in permissions:
        if not isinstance(p, dict) or str(p.get("type", "")).lower() != "domain":
            continue
        if domain and str(p.get("emailAddress", p.get("domain", ""))).lower() == domain:
            return {"status": "shared", "audience": "internal", "via": "drive:domain"}
    people = [p for p in permissions if isinstance(p, dict)
              and str(p.get("type", "")).lower() in ("user", "group")
              and str(p.get("role", "")).lower() != "owner"]
    if people:
        return {"status": "private", "audience": "internal", "via": "drive:users",
                "note": f"shared with {len(people)} specific people or groups, not the domain"}
    return {"status": "private", "audience": "internal", "via": "drive:owner-only"}


def notion_verdict(url: str, private_ids: list, default_status: str,
                   ancestor_ids: list = None, private_titles: list = None,
                   ancestor_titles: list = None) -> dict:
    """Private-section proxy.

    A page in the owner's Private list, or one whose ancestors include such a
    page, is private; the rest get ``default_status``. The ancestor check
    needs the page's ancestor path (from a fetch), so it runs per link at
    answer time, not in the bulk pass.
    """
    pid = link_ids(url).get("notion_page_id")
    norm = {str(x).replace("-", "").lower() for x in private_ids}
    if pid and pid in norm:
        return {"status": "private", "audience": "internal", "via": "notion:private-section"}
    for a in ancestor_ids or []:
        if str(a).replace("-", "").lower() in norm:
            return {"status": "private", "audience": "internal", "via": "notion:private-ancestor"}
    titles = {str(t).strip().lower() for t in private_titles or [] if str(t).strip()}
    for a in ancestor_titles or []:
        if str(a).strip().lower() in titles:
            return {"status": "private", "audience": "internal", "via": "notion:private-ancestor"}
    checked = ancestor_ids is not None or ancestor_titles is not None
    via = "notion:not-private" + ("-checked" if checked else "")
    return {"status": default_status, "audience": "internal", "via": via,
            "note": "proxy: not in the owner's Private section"
                    + ("" if checked else "; ancestors unchecked")}


def _render_registry(entries: list, body: str) -> str:
    """Rewrite the first table in ``body`` from ``entries`` (REGISTRY_COLUMNS order)."""
    lines = body.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip().startswith("|")), None)
    end = start
    if start is not None:
        while end + 1 < len(lines) and lines[end + 1].strip().startswith("|"):
            end += 1
    header = "| " + " | ".join("URL" if c == "url" else c.title() for c in REGISTRY_COLUMNS) + " |"
    sep = "|" + "---|" * len(REGISTRY_COLUMNS)
    rows = []
    for e in entries:
        cells = []
        for c in REGISTRY_COLUMNS:
            v = e.get(c, "")
            if c == "audience" and isinstance(v, list):
                v = ", ".join(v)
            cells.append(str(v).replace("|", "\\|"))
        rows.append("| " + " | ".join(cells) + " |")
    table = [header, sep] + rows
    if start is None:
        return body.rstrip("\n") + "\n\n" + "\n".join(table) + "\n"
    return "\n".join(lines[:start] + table + lines[end + 1:]) + "\n"


def save_registry(target: Path, entries: list) -> None:
    p = respond_sources_path(target)
    text = _read(p)
    fm, body = parse_frontmatter(text)
    head = text[: len(text) - len(body)] if body else text
    head = re.sub(r"^updated:.*$", f"updated: {_now().date().isoformat()}", head, count=1, flags=re.M)
    _write_atomic(p, head + _render_registry(entries, body))


def set_registry_row(entries: list, url: str, verdict: dict, force: bool = False) -> dict:
    """Apply a verdict to the row for ``url`` (appending one if missing). Manual rows are kept."""
    norm = normalize_url(url)
    row = next((e for e in entries if e["url_norm"] == norm), None)
    if row is None:
        row = {"title": "", "url": _clean_url(url), "kind": _kind_for(url), "audience": ["internal"],
               "status": "unknown", "verified": "", "via": "", "note": "", "url_norm": norm}
        entries.append(row)
    if row.get("via") == MANUAL_VIA and not force:
        return {"url": row["url"], "kept": True, "reason": "manual row; pass --force to overwrite"}
    for k in ("status", "audience", "via", "note", "title", "kind"):
        if verdict.get(k):
            row[k] = verdict[k]
    if isinstance(row.get("audience"), str):
        row["audience"] = [a.strip().lower() for a in row["audience"].split(",") if a.strip()]
    row["verified"] = verdict.get("verified") or _now().date().isoformat()
    return {"url": row["url"], "kept": False, "status": row["status"], "audience": row["audience"],
            "via": row["via"]}


def corpus_links(target: Path, policy: dict) -> dict:
    """url_norm -> {url, kind, cited_by} for every link the corpus pages cite (people pages excluded)."""
    urls = {}
    for rec in compute_corpus(target, policy)["pages"]:
        try:
            body = cq._safe_frontmatter(_read(target / rec["path"]))[1]  # pylint: disable=protected-access
        except OSError:
            continue
        for u in URL_RE.findall(body):
            norm = normalize_url(u)
            if norm not in urls:
                urls[norm] = {"url": _clean_url(u), "kind": _kind_for(u), "cited_by": rec["slug"]}
    return urls


def seed_registry(target: Path, policy: dict, entries: list) -> list:
    """Append an unknown row for each corpus link the registry lacks; return the urls added."""
    have = {e["url_norm"] for e in entries}
    added = []
    for norm, e in sorted(corpus_links(target, policy).items()):
        if norm in have:
            continue
        entries.append({"title": "", "url": e["url"], "kind": e["kind"], "audience": ["internal"],
                        "status": "unknown", "verified": "", "via": "", "note": f"cited by {e['cited_by']}",
                        "url_norm": norm})
        added.append(e["url"])
    return added


def apply_kind_rules(entries: list, policy: dict) -> list:
    rules = parse_kind_rules(policy)
    changed = []
    for e in entries:
        rule = rules.get(e["kind"])
        if not rule or e.get("via") == MANUAL_VIA:
            continue
        status, audience = rule
        if e["status"] == status and e["audience"] == [audience] and not is_stale(e, policy):
            continue
        set_registry_row(entries, e["url"], {"status": status, "audience": audience, "via": f"rule:{e['kind']}"})
        changed.append(e["url"])
    return changed


def shareable(entries: list, policy: dict) -> dict:
    """url_norm -> entry for every registry row that may be linked."""
    allowed = {a.lower() for a in policy["link_audiences"]}
    out = {}
    for e in entries:
        if e["status"] != "shared" or is_stale(e, policy):
            continue
        if not (set(e["audience"]) & allowed):
            continue
        out[e["url_norm"]] = e
    return out


def lessons_summary(target: Path) -> dict:
    p = respond_lessons_path(target)
    if not p.is_file():
        return {"present": False}
    fm, body = parse_frontmatter(_read(p))
    counts = {}
    current = None
    fenced = False
    for line in body.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if line.startswith("## "):
            current = line[3:].strip()
            counts[current] = 0
        elif line.startswith("### ") and current:
            counts[current] += 1
    return {"present": True, "type": fm.get("type"), "sections": counts}


# ---------- corpus ----------

def _layers(target: Path) -> list:
    return cq._readable_layers(target)  # pylint: disable=protected-access


def _index_summaries(wiki: Path) -> dict:
    idx = wiki / INDEX_FILE
    if not idx.is_file():
        return {}
    out = {}
    for line in _read(idx).splitlines():
        m = INDEX_BULLET_RE.match(line)
        if m:
            out[m.group(1).strip()] = m.group(2).strip()
    return out


def compute_corpus(target: Path, policy: dict) -> dict:
    """Allowed pages after the policy filters, nearest layer first; excluded pages with reasons."""
    cq._wiki_root(target)  # pylint: disable=protected-access
    allow_types = set(policy["allow_types"])
    deny_slugs = set(policy["deny_slugs"])
    allow_slugs = set(policy["allow_slugs"])  # wins over source patterns, never over deny_slugs
    patterns = [p.lower() for p in policy["deny_source_patterns"] if p]
    pages, excluded, seen = [], [], set()
    for layer in _layers(target):
        summaries = _index_summaries(layer["wiki"])
        for slug, p, default_type in cq._iter_all_pages(layer["wiki"]):  # pylint: disable=protected-access
            if slug in ("index", "log") or slug in seen:
                continue
            seen.add(slug)
            try:
                text = _read(p)
            except OSError:
                continue
            fm, body = cq._safe_frontmatter(text)  # pylint: disable=protected-access
            ptype = fm.get("type") if isinstance(fm.get("type"), str) else default_type
            title = cq._title_from(fm, body, slug)  # pylint: disable=protected-access
            reasons = []
            if ptype not in allow_types:
                reasons.append(f"type {ptype} not allowed")
            if slug in deny_slugs:
                reasons.append("listed in deny_slugs")
            for src in ([] if slug in allow_slugs else _as_list(fm.get("sources"))):
                hit = next((pat for pat in patterns if pat in src.lower()), None)
                if hit:
                    reasons.append(f"source {src} matches {hit}")
                    break
            rec = {
                "slug": slug,
                "type": ptype,
                "title": title,
                "summary": summaries.get(slug, ""),
                "path": cq._rel_or_abs(p, target),  # pylint: disable=protected-access
                "layer": layer.get("id"),
                "updated": fm.get("updated", ""),
            }
            if reasons:
                rec["reasons"] = reasons
                excluded.append(rec)
            else:
                pages.append(rec)
    return {
        "pages": pages,
        "excluded": excluded,
        "counts": {"allowed": len(pages), "excluded": len(excluded)},
    }


def corpus_slugs(target: Path, policy: dict) -> set:
    return {p["slug"] for p in compute_corpus(target, policy)["pages"]}


# ---------- state ----------

def _empty_state() -> dict:
    return {
        "window": None,
        "last_tick": None,
        "tick_started": None,
        "ticks": 0,
        "tick_posts": 0,
        "last_seen": {},
        "handled": {},
        "clarifications": {},
        "counters": {},
        "negatives": 0,
        "tightened": False,
        "next_id": 1,
    }


def load_state(target: Path) -> dict:
    p = respond_state_path(target)
    if not p.is_file():
        return _empty_state()
    try:
        data = json.loads(_read(p))
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    state = _empty_state()
    state.update(data if isinstance(data, dict) else {})
    return state


def save_state(target: Path, state: dict) -> None:
    _write_atomic(respond_state_path(target), json.dumps(state, indent=2, sort_keys=True) + "\n")


def _window(state: dict) -> dict | None:
    w = state.get("window")
    return w if isinstance(w, dict) else None


def window_open(state: dict) -> bool:
    w = _window(state)
    return bool(w and w.get("open"))


def window_past_end(state: dict) -> bool:
    w = _window(state)
    if not w or not w.get("until"):
        return False
    return _now() >= _parse_when(w["until"])


def _prune_handled(state: dict) -> None:
    cutoff = (_now() - timedelta(days=HANDLED_RETENTION_DAYS)).timestamp()
    keep = {}
    for key, val in state.get("handled", {}).items():
        try:
            ts = float(key.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            keep[key] = val
            continue
        if ts >= cutoff:
            keep[key] = val
    state["handled"] = keep


# ---------- ledger ----------

def _ledger_text(target: Path) -> str:
    p = respond_ledger_path(target)
    if not p.is_file():
        raise Refusal(f"missing {p}")
    return _read(p)


def parse_ledger(text: str) -> list:
    """Return the ledger's entries as dicts (header fields plus ``- key: value`` lines)."""
    entries = []
    matches = list(ENTRY_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[m.end():end]
        entry = {
            "id": m.group("id"),
            "ts": m.group("ts"),
            "outcome": m.group("outcome"),
            "intent": m.group("intent").strip(),
            "confidence": m.group("confidence").strip(),
        }
        for line in block.splitlines():
            if not line.startswith("- ") or ":" not in line:
                continue
            key, _, val = line[2:].partition(":")
            key, val = key.strip(), val.strip()
            entry[key] = _parse_value(val, 0) if val.startswith("[") else val
        entries.append(entry)
    return entries


def _format_entry(entry: dict) -> str:
    def lst(v):
        return "[" + ", ".join(_as_list(v)) + "]"
    lines = [
        f"## [{entry['ts']}] {entry['id']} | {entry['outcome']} | "
        f"{entry.get('intent', '')} | {entry.get('confidence', '')}",
        "",
        f"- channel: {entry.get('channel', '')}",
        f"- thread: {entry.get('thread_key', '')}",
        f"- asker: {entry.get('asker', '')}",
        f"- question: {entry.get('question', '')}",
        f"- pages: {lst(entry.get('pages'))}",
        f"- links: {lst(entry.get('links'))}",
        f"- reply_ts: {entry.get('reply_ts', '')}",
        f"- mode: {entry.get('mode', '')}",
        f"- escalated_via: {', '.join(_as_list(entry.get('escalated_via')))}",
        f"- house_answer: {'true' if entry.get('house_answer') else 'false'}",
        "- reaction: ",
        "- grade: ",
        "- note: ",
        "",
    ]
    return "\n".join(lines) + "\n"


def _update_entry_field(text: str, entry_id: str, field: str, value: str) -> str:
    matches = list(ENTRY_RE.finditer(text))
    for i, m in enumerate(matches):
        if m.group("id") != entry_id:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[m.start():end]
        pat = re.compile(rf"^- {re.escape(field)}:.*$", re.M)
        if pat.search(block):
            new_block = pat.sub(f"- {field}: {value}", block, count=1)
        else:
            new_block = block.rstrip("\n") + f"\n- {field}: {value}\n\n"
        return text[:m.start()] + new_block + text[end:]
    raise Refusal(f"no ledger entry {entry_id}")


# ---------- validation (the gate) ----------

def validate_draft(target: Path, policy: dict, state: dict, draft: dict,
                   offline: bool = False, follow_up: bool = False) -> dict:
    """Return {"ok", "problems", "links"}; never raises on a bad draft."""
    problems = []
    outcome = str(draft.get("outcome", "")).strip()
    message = str(draft.get("message", "") or "")
    intent = str(draft.get("intent", "") or "").strip()
    confidence = str(draft.get("confidence", "") or "").strip().lower()
    key = str(draft.get("thread_key", "") or "").strip()
    asker = str(draft.get("asker", "") or "").strip()
    asker_email = str(draft.get("asker_email", "") or "").strip().lower()
    pages = _as_list(draft.get("pages"))
    links = _as_list(draft.get("links"))
    house = bool(draft.get("house_answer"))

    if outcome not in ALL_OUTCOMES:
        problems.append(f"outcome must be one of {list(ALL_OUTCOMES)}")
        return {"ok": False, "problems": problems, "links": []}
    if not key:
        problems.append("thread_key is required (channel_id:message_ts)")

    # Window and caps (skipped offline: tests and replay).
    if not offline:
        if not window_open(state):
            problems.append("no open window (run `window start`)")
        elif window_past_end(state):
            problems.append("window is past its end (run `window stop`)")
        if outcome in POSTED_OUTCOMES and state.get("tick_posts", 0) >= int(policy["max_posts_per_tick"]):
            problems.append(f"per-tick cap reached ({policy['max_posts_per_tick']}); escalate as high volume")

    # Who. In a test channel the owner plays the asker.
    channel_id = key.split(":", 1)[0] if key else ""
    in_test_channel = channel_id in set(policy.get("test_channels", []))
    owner = str(policy.get("owner_slack_id") or "")
    if owner and asker == owner and not in_test_channel:
        problems.append("asker is the owner; never answer the owner's own messages")
    domain = str(policy.get("internal_domain") or "").lower()
    if asker_email and domain and not asker_email.endswith("@" + domain):
        problems.append(f"asker is not internal ({asker_email})")
    if draft.get("channel_external"):
        problems.append("channel is externally shared")

    # Idempotency and clarification state.
    handled = state.get("handled", {})
    clar = state.get("clarifications", {}).get(key)
    if key in handled and outcome in POSTED_OUTCOMES:
        open_clar = bool(clar) and not clar.get("resolved")
        if not (open_clar or follow_up):
            problems.append(f"thread {key} already handled ({handled[key]})")
    if outcome == "clarified":
        count = int(clar.get("count", 0)) if clar else 0
        if count >= int(policy["max_clarifications_per_thread"]):
            problems.append("clarification limit reached for this thread; escalate")

    # Tiers and intents.
    if outcome == "answered" and confidence != "high":
        problems.append("answered requires confidence high")
    if outcome == "clarified" and confidence != "medium":
        problems.append("clarified requires confidence medium")
    if outcome in ("answered", "clarified") and intent not in policy["intents"]:
        problems.append(f"intent {intent!r} is not supported; escalate instead")
    if state.get("tightened") and outcome == "answered" and not house:
        problems.append("tightened: only house answers may be posted until the next review")

    # Message shape and content (posted outcomes only).
    if outcome in POSTED_OUTCOMES:
        header = str(policy["header"])
        if not message.lstrip().startswith(header):
            problems.append("message must start with the policy header")
        if outcome == "answered" and str(policy["footer"]) not in message:
            problems.append("answered replies must carry the policy footer")
        n_lines = len(message.splitlines())
        if n_lines > int(policy["max_reply_lines"]):
            problems.append(f"message has {n_lines} lines; max {policy['max_reply_lines']}")
        if len(message) > int(policy["max_reply_chars"]):
            problems.append(f"message has {len(message)} chars; max {policy['max_reply_chars']}")
        low = message.lower()
        for phrase in policy["never_say"]:
            if phrase and phrase.lower() in low:
                problems.append(f"message contains a never_say phrase: {phrase!r}")
        for phrase in policy["forbidden_phrases"]:
            if phrase and phrase.lower() in low:
                problems.append(f"message speaks as the owner: {phrase!r}")

    # Links: everything in the message and in ``links`` must be shareable.
    checked_links = []
    try:
        entries, _ = load_registry(target)
        ok_links = shareable(entries, policy)
    except Refusal as e:
        entries, ok_links = [], {}
        problems.append(str(e))
    by_norm = {e["url_norm"]: e for e in entries} if ok_links or entries else {}
    found = [normalize_url(u) for u in URL_RE.findall(message)] + [normalize_url(u) for u in links]
    for u in dict.fromkeys(found):
        if u in ok_links:
            checked_links.append({"url": u, "title": ok_links[u].get("title", ""), "via": ok_links[u].get("via", "")})
            continue
        row = by_norm.get(u)
        if row is not None and row["status"] == "shared" and is_stale(row, policy):
            problems.append(f"link verification is stale (run verify): {u}")
        else:
            problems.append(f"link not in the registry's shared set: {u}")

    # Pages: everything cited must be in the corpus.
    if pages:
        try:
            allowed = corpus_slugs(target, policy)
        except cq.ToolError as e:
            allowed = set()
            problems.append(str(e))
        for s in pages:
            if s not in allowed:
                problems.append(f"page not in the corpus: {s}")

    return {"ok": not problems, "problems": problems, "links": checked_links}


# ---------- subcommands ----------

def cmd_init(target: Path, args) -> dict:
    rd = respond_dir(target)
    if rd.exists() and any(rd.iterdir()) and not args.force:
        raise Refusal(f"{rd} already exists; pass --force to rewrite the templates (ledger and state are kept)")
    policy = dict(DEFAULT_POLICY)
    owner = args.owner_name or policy["owner_name"]
    policy.update({
        "owner_name": owner,
        "owner_slack_id": args.owner_slack_id or "",
        "internal_domain": args.internal_domain or "",
        "roadmap_page": args.roadmap_page or "",
        "header": f":robot_face: *Automated reply from {owner}'s stand-in (this is not {owner}).*",
        "footer": f"_Useful? React :+1: or :-1: so {owner} can tune me._",
    })
    # Seeds: the corpus under the default filters, people pages, and the links the allowed pages cite.
    corpus = compute_corpus(target, policy)
    people = []
    for rec in corpus["pages"]:
        if rec["type"] == "entity" and PERSON_TITLE_RE.match(rec["title"] or ""):
            people.append(rec["slug"])
    urls = {}
    for rec in corpus["pages"]:
        if rec["slug"] in people:
            continue
        try:
            body = cq._safe_frontmatter(_read(target / rec["path"]))[1]  # pylint: disable=protected-access
        except OSError:
            continue
        for u in URL_RE.findall(body):
            norm = normalize_url(u)
            if norm not in urls:
                urls[norm] = {"url": _clean_url(u), "kind": _kind_for(u), "cited_by": rec["slug"]}
    kind_order = ["gdoc", "gslides", "gsheet", "claude-artifact", "claude-design", "linear",
                  "storybook", "figma", "public", "notion"]
    rows = []
    for norm in sorted(urls, key=lambda n: (kind_order.index(urls[n]["kind"])
                                            if urls[n]["kind"] in kind_order else 99, n)):
        e = urls[norm]
        rows.append(f"| | {e['url']} | {e['kind']} | internal | unknown | | | cited by {e['cited_by']} |")
    checklist = [
        f"- `deny_slugs` seeds {len(people)} entity page(s) whose title reads like a person's name "
        f"(organizations can match too): {', '.join(people) or 'none'}. Keep people; trim the rest or keep "
        "them if customer pages should stay out.",
        f"- {len(corpus['excluded'])} page(s) are excluded by type or by a source pattern; `corpus` lists them with reasons.",
        f"- The registry seeds {len(rows)} link(s) with status `unknown`; set `shared` only for what is actually shared with the audience.",
        "- Fill `owner_slack_id`, `internal_domain`, `imessage_to` (if used), `escalation_channel`, `working_hours`, and `never_say` (customer names, figures).",
        "- Read the Never list once and add what is missing for this project.",
    ]
    ctx = {
        "project_name": args.project_name or target.resolve().name,
        "date": _now().date().isoformat(),
        "owner_name": owner,
        "owner_slack_id": policy["owner_slack_id"],
        "internal_domain": policy["internal_domain"],
        "roadmap_page": policy["roadmap_page"],
        "header": policy["header"],
        "footer": policy["footer"],
        "deny_slugs": "[" + ", ".join(people) + "]",
        "registry_rows": "\n".join(rows) if rows else "| | | | | | | | |",
        "review_checklist": "\n".join(checklist),
    }
    rd.mkdir(parents=True, exist_ok=True)
    created = []
    for name, path in (
        ("respond-policy.md", respond_policy_path(target)),
        ("respond-sources.md", respond_sources_path(target)),
        ("respond-lessons.md", respond_lessons_path(target)),
    ):
        _write_atomic(path, _render(name, ctx))
        created.append(cq._rel_or_abs(path, target))  # pylint: disable=protected-access
    ledger = respond_ledger_path(target)
    if not ledger.is_file():  # never rewritten, not even with --force
        _write_atomic(ledger, _render("respond-ledger.md", ctx))
        created.append(cq._rel_or_abs(ledger, target))  # pylint: disable=protected-access
    if not respond_state_path(target).is_file():
        save_state(target, _empty_state())
    entries, _ = load_registry(target)
    ruled = apply_kind_rules(entries, {**policy, "kind_rules": _as_list(policy["kind_rules"])})
    if ruled:
        save_registry(target, entries)
    # schema.md gains a Respond section once (co-evolved file).
    sp = schema_path(target)
    schema_note = False
    if sp.is_file() and "## Respond" not in _read(sp):
        _write_atomic(sp, _read(sp).rstrip("\n") + "\n\n" + _render("respond-schema-section.md", ctx))
        schema_note = True
    return {
        "created": created,
        "deny_slugs_seeded": people,
        "excluded_by_filters": [{"slug": r["slug"], "reasons": r["reasons"]} for r in corpus["excluded"]],
        "registry_seeded": len(rows),
        "registry_ruled": len(ruled),
        "schema_section_added": schema_note,
        "review": checklist,
    }


def cmd_corpus(target: Path, _args) -> dict:
    policy, _ = load_policy(target)
    return compute_corpus(target, policy)


def cmd_search(target: Path, args) -> dict:
    policy, _ = load_policy(target)
    allowed = {p["slug"]: p for p in compute_corpus(target, policy)["pages"]}
    merged = {}
    for q in args.query:
        res = cq.tool_compathy_search(target, {"query": q, "max_results": 200})
        short = len(q.strip()) <= 4  # an acronym: "HOD" must not match "method"
        word = re.compile(r"(?<![A-Za-z0-9])" + re.escape(q.strip()) + r"(?![A-Za-z0-9])", re.I)
        for r in res["results"]:
            if r["slug"] not in allowed:
                continue
            if short:
                try:
                    text = _read(target / allowed[r["slug"]]["path"])
                except OSError:
                    continue
                if not word.search(text):
                    continue
            cur = merged.get(r["slug"])
            if cur is None or r["score"] > cur["score"]:
                r = dict(r)
                r["summary"] = allowed[r["slug"]]["summary"]
                r["queries"] = sorted(set((cur or {}).get("queries", [])) | {q})
                merged[r["slug"]] = r
            else:
                cur["queries"] = sorted(set(cur["queries"]) | {q})
    results = sorted(merged.values(), key=lambda m: (-m["score"], m["slug"]))[: args.max_results]
    return {"queries": args.query, "count": len(results), "results": results}


def cmd_page(target: Path, args) -> dict:
    policy, _ = load_policy(target)
    corpus = compute_corpus(target, policy)
    allowed = {p["slug"] for p in corpus["pages"]}
    if args.slug not in allowed:
        reason = next((r["reasons"] for r in corpus["excluded"] if r["slug"] == args.slug), None)
        raise Refusal(f"page {args.slug!r} is not in the corpus"
                      + (f": {'; '.join(reason)}" if reason else " (no such page)"))
    page = cq.tool_compathy_get_page(target, {"slug": args.slug})
    entries, _ = load_registry(target)
    by_norm = {e["url_norm"]: e for e in entries}
    ok = shareable(entries, policy)
    links = []
    for u in dict.fromkeys(_clean_url(u) for u in URL_RE.findall(page["body"])):
        norm = normalize_url(u)
        e = by_norm.get(norm)
        links.append({
            "url": u,
            "registry": "unregistered" if e is None else e["status"],
            "shareable": norm in ok,
            "title": (e or {}).get("title", ""),
        })
    return {
        "slug": page["slug"],
        "title": page["title"],
        "type": page["page_type"],
        "layer": page["layer"],
        "updated": page["frontmatter"].get("updated", ""),
        "sources": _as_list(page["frontmatter"].get("sources")),
        "body": page["body"],
        "links": links,
    }


def cmd_sources(target: Path, args) -> dict:
    policy, _ = load_policy(target)
    entries, _ = load_registry(target)
    action = getattr(args, "action", None) or "list"
    if action == "seed":
        added = seed_registry(target, policy, entries)
        if added:
            save_registry(target, entries)
        return {"added": added, "count": len(added),
                "hint": "new rows are unknown; run apply-rules and verify before they can be linked"}
    if action == "apply-rules":
        changed = apply_kind_rules(entries, policy)
        if changed:
            save_registry(target, entries)
        return {"changed": changed, "count": len(changed)}
    if action == "set":
        if not args.url:
            raise Refusal("--url is required")
        verdict = {"status": args.status, "audience": args.audience, "via": args.via or MANUAL_VIA,
                   "note": args.note, "title": args.title, "kind": args.kind}
        if verdict["status"] and verdict["status"] not in REGISTRY_STATUSES:
            raise Refusal(f"--status must be one of {list(REGISTRY_STATUSES)}")
        res = set_registry_row(entries, args.url, verdict, force=args.force)
        if not res.get("kept"):
            save_registry(target, entries)
        return res
    if action == "verdict":
        data = _load_json_arg(args.json)
        if args.kind in DRIVE_KINDS or args.kind == "drive":
            if not args.url:
                raise Refusal("--url is required for a drive verdict")
            perms = data.get("permissions", data if isinstance(data, list) else [])
            verdict = drive_verdict(perms if isinstance(perms, list) else [], policy["internal_domain"])
            targets = [args.url]
        elif args.kind == "notion":
            private_ids = data.get("private_ids", [])
            targets = [args.url] if args.url else [e["url"] for e in entries if e["kind"] == "notion"]
            verdict = None
        else:
            raise Refusal("--kind must be gdoc, gsheet, gslides, drive or notion")
        results = []
        if verdict is not None and args.title:
            verdict["title"] = args.title
        scoped = args.kind == "notion" and bool(args.url)
        ancestors = data.get("ancestor_ids") if scoped else None
        ancestor_titles = data.get("ancestor_titles") if scoped else None
        if scoped and ancestor_titles is None and isinstance(data.get("path"), str):
            ancestor_titles = [t.strip() for t in data["path"].split("/") if t.strip()]
        private_titles = data.get("private_titles", []) if args.kind == "notion" else []
        for u in targets:
            v = verdict or notion_verdict(u, private_ids, str(policy["notion_default"]), ancestors,
                                          private_titles, ancestor_titles)
            res = set_registry_row(entries, u, v, force=args.force)
            res["via"] = v["via"]
            results.append(res)
        if args.apply:
            save_registry(target, entries)
        return {"applied": bool(args.apply), "results": results,
                "hint": "" if args.apply else "pass --apply to write the registry"}
    ok = shareable(entries, policy)
    out = []
    for e in entries:
        if args.status and e["status"] != args.status:
            continue
        if args.needs_verification:
            manual_pending = e["kind"] in MANUAL_KINDS and e["status"] == "unknown"
            needs = e.get("via") != MANUAL_VIA and (e["status"] == "unknown" or is_stale(e, policy))
            if not needs:
                continue
            e = dict(e)
            e["verifier"] = ("drive" if e["kind"] in DRIVE_KINDS else
                             "notion" if e["kind"] == "notion" else
                             "rule" if e["kind"] in parse_kind_rules(policy) else
                             "manual" if manual_pending or e["kind"] in MANUAL_KINDS else "manual")
            e.update(link_ids(e["url"]))
        if args.match:
            hay = " ".join([e.get("title", ""), e.get("note", ""), e.get("url", ""), e.get("kind", "")]).lower()
            if args.match.lower() not in hay:
                continue
        rec = {k: v for k, v in e.items() if k != "url_norm"}
        rec["shareable"] = e["url_norm"] in ok
        rec["stale"] = e["status"] == "shared" and is_stale(e, policy)
        out.append(rec)
    summary = {}
    for e in entries:
        summary[e["status"]] = summary.get(e["status"], 0) + 1
    return {"count": len(out), "entries": out, "by_status": summary,
            "max_age_days": policy["link_verify_max_age_days"]}


def _handoff(target: Path, state: dict, policy: dict) -> Path:
    w = _window(state) or {}
    started = w.get("started", "")
    entries = [e for e in parse_ledger(_ledger_text(target)) if e["ts"] >= started]
    by = {}
    for e in entries:
        by.setdefault(e["outcome"], []).append(e)
    today = _now().date().isoformat()
    lines = [
        "---", "type: respond-handoff", "schema_version: 1",
        f"window_started: {started}", f"window_until: {w.get('until', '')}",
        f"stopped: {_iso(_now())}", f"mode: {w.get('mode', '')}", "---", "",
        f"# Stand-in handoff for {policy['owner_name']}, {today}", "",
        "## Numbers", "",
    ]
    counters = state.get("counters", {})
    for o in ALL_OUTCOMES:
        n = int(counters.get("ignored", 0)) if o == "ignored" else len(by.get(o, []))
        lines.append(f"- {o}: {n}")
    open_clar = [k for k, v in state.get("clarifications", {}).items() if not v.get("resolved")]
    lines += ["", f"- clarifications still open: {len(open_clar)}", ""]
    for title, outcome in (("Escalated (needs you)", "escalated"), ("Clarified", "clarified"),
                           ("Answered", "answered"), ("Handed over", "handed-over")):
        lines += [f"## {title}", ""]
        items = by.get(outcome, [])
        if not items:
            lines.append("_(none)_")
        for e in items:
            lines.append(f"- {e['id']} {e.get('channel', '')} {e.get('thread', '')}: "
                         f"{e.get('question', '')} (asker {e.get('asker', '')}, {e.get('intent', '')})")
        lines.append("")
    path = respond_dir(target) / f"handoff-{today}.md"
    _write_atomic(path, "\n".join(lines))
    return path


def cmd_window(target: Path, args) -> dict:
    policy, _ = load_policy(target)
    state = load_state(target)
    if args.action == "status":
        w = _window(state)
        out = {"window": w, "open": window_open(state), "past_end": window_past_end(state),
               "counters": state.get("counters", {}), "tightened": state.get("tightened", False),
               "negatives": state.get("negatives", 0), "ticks": state.get("ticks", 0),
               "last_tick": state.get("last_tick"),
               "open_clarifications": [k for k, v in state.get("clarifications", {}).items()
                                       if not v.get("resolved")]}
        if w and w.get("until"):
            out["remaining_seconds"] = max(0, int((_parse_when(w["until"]) - _now()).total_seconds()))
        return out
    if args.action == "start":
        if window_open(state) and not window_past_end(state):
            raise Refusal(f"a window is already open until {state['window']['until']}; run `window stop` first")
        if not args.until:
            raise Refusal("--until <ISO date-time> is required")
        until = _parse_when(args.until)
        if until <= _now():
            raise Refusal(f"--until {args.until} is in the past")
        mode = args.mode or "dry"
        if mode not in ("dry", "draft", "live"):
            raise Refusal("--mode must be dry, draft or live")
        every = args.every or "5m"
        _prune_handled(state)
        state["window"] = {
            "open": True,
            "started": _iso(_now()),
            "until": _iso(until),
            "every": every,
            "every_seconds": _parse_every(every),
            "mode": mode,
            "scope": _csv(args.scope) or policy["triggers"],
            "channels": _csv(args.channels),
            "route": _csv(args.route) or policy["escalation_routes"],
            "task_id": args.task_id or None,
        }
        state.update({"counters": {}, "negatives": 0, "tightened": False, "tick_posts": 0,
                      "clarifications": {}, "ticks": 0, "last_tick": None, "tick_started": None})
        save_state(target, state)
        return {"started": True, "window": state["window"]}
    if args.action == "update":
        w = _window(state)
        if not w:
            raise Refusal("no window to update")
        if args.task_id is not None:
            w["task_id"] = args.task_id
        if args.until:
            until = _parse_when(args.until)
            if until <= _now():
                raise Refusal(f"--until {args.until} is in the past")
            w["until"] = _iso(until)
            w["open"] = True
        save_state(target, state)
        return {"window": w}
    if args.action == "stop":
        w = _window(state)
        if not w:
            raise Refusal("no window to stop")
        path = _handoff(target, state, policy)
        w["open"] = False
        w["stopped"] = _iso(_now())
        task_id = w.get("task_id")
        save_state(target, state)
        return {"stopped": True, "handoff": cq._rel_or_abs(path, target),  # pylint: disable=protected-access
                "task_id": task_id, "counters": state.get("counters", {})}
    raise Refusal(f"unknown window action {args.action!r}")


def cmd_tick(target: Path, args) -> dict:
    policy, _ = load_policy(target)
    state = load_state(target)
    if args.action == "begin":
        if not window_open(state):
            return {"open": False, "action": "exit", "reason": "no open window"}
        if window_past_end(state):
            return {"open": False, "action": "stop", "reason": "window past its end",
                    "until": state["window"]["until"]}
        now = _now()
        if args.since:
            since = now - timedelta(seconds=_parse_every(args.since))
        elif state.get("last_tick"):
            since = _parse_when(state["last_tick"]) - timedelta(seconds=OVERLAP_SECONDS)
        else:
            since = _parse_when(state["window"]["started"])
        started = _parse_when(state["window"]["started"])
        floor = min(since, started) if args.since else max(since, started - timedelta(seconds=OVERLAP_SECONDS))
        state["tick_started"] = _iso(now)
        state["tick_posts"] = 0
        save_state(target, state)
        w = state["window"]
        return {
            "open": True,
            "action": "run",
            "since_iso": _iso(floor),
            "since_epoch": int(floor.timestamp()),
            "since_date": floor.date().isoformat(),
            "mode": w["mode"],
            "scope": w["scope"],
            "channels": w["channels"],
            "route": w["route"],
            "until": w["until"],
            "owner_slack_id": policy["owner_slack_id"],
            "owner_name": policy["owner_name"],
            "internal_domain": policy["internal_domain"],
            "intents": policy["intents"],
            "max_posts_per_tick": policy["max_posts_per_tick"],
            "tightened": state.get("tightened", False),
            "handled_keys": sorted(state.get("handled", {})),
            "open_clarifications": {k: v for k, v in state.get("clarifications", {}).items()
                                    if not v.get("resolved")},
            "lessons": lessons_summary(target),
            "ticks_so_far": state.get("ticks", 0),
        }
    if args.action == "end":
        state["last_tick"] = state.get("tick_started") or _iso(_now())
        state["ticks"] = int(state.get("ticks", 0)) + 1
        state["tick_started"] = None
        save_state(target, state)
        return {"ended": True, "ticks": state["ticks"], "last_tick": state["last_tick"],
                "posts_this_tick": state.get("tick_posts", 0)}
    raise Refusal(f"unknown tick action {args.action!r}")


def _load_json_arg(s: str) -> dict:
    try:
        data = json.loads(s)
    except json.JSONDecodeError as e:
        raise Refusal(f"--json is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise Refusal("--json must be an object")
    return data


def cmd_check(target: Path, args) -> dict:
    policy, _ = load_policy(target)
    state = load_state(target)
    draft = _load_json_arg(args.json)
    result = validate_draft(target, policy, state, draft, offline=args.offline, follow_up=args.follow_up)
    result["outcome"] = draft.get("outcome")
    result["thread_key"] = draft.get("thread_key")
    return result


def cmd_record(target: Path, args) -> dict:
    policy, _ = load_policy(target)
    state = load_state(target)
    data = _load_json_arg(args.json)
    outcome = str(data.get("outcome", "")).strip()
    key = str(data.get("thread_key", "") or "").strip()
    if outcome not in ALL_OUTCOMES:
        raise Refusal(f"outcome must be one of {list(ALL_OUTCOMES)}")
    if not key:
        raise Refusal("thread_key is required")
    if outcome == "ignored":
        state["handled"][key] = f"ignored:{data.get('reason', '')}"
        state["counters"]["ignored"] = int(state["counters"].get("ignored", 0)) + 1
        save_state(target, state)
        return {"recorded": True, "outcome": "ignored", "thread_key": key, "ledger": False}
    imported = bool(getattr(args, "imported", False))
    if imported:
        if key in state.get("handled", {}):
            return {"recorded": False, "skipped": True, "thread_key": key, "reason": "already in the ledger"}
    elif outcome in POSTED_OUTCOMES:
        check = validate_draft(target, policy, state, data, offline=args.offline, follow_up=args.follow_up)
        if not check["ok"]:
            return {"recorded": False, "problems": check["problems"], "exit": 1}
    entry_id = f"R-{int(state.get('next_id', 1)):04d}"
    entry = {
        "id": entry_id,
        "ts": _iso(_now()),
        "outcome": outcome,
        "intent": data.get("intent", ""),
        "confidence": data.get("confidence", ""),
        "channel": data.get("channel", ""),
        "thread_key": key,
        "asker": data.get("asker", ""),
        "question": str(data.get("question", "")).replace("\n", " ").strip(),
        "pages": _as_list(data.get("pages")),
        "links": [normalize_url(u) for u in _as_list(data.get("links"))]
                 or [normalize_url(u) for u in URL_RE.findall(str(data.get("message", "")))],
        "reply_ts": data.get("reply_ts", ""),
        "mode": data.get("mode") or ("imported" if imported else (_window(state) or {}).get("mode", "")),
        "escalated_via": _as_list(data.get("escalated_via")),
        "house_answer": bool(data.get("house_answer")),
    }
    ledger = respond_ledger_path(target)
    _write_atomic(ledger, _ledger_text(target).rstrip("\n") + "\n\n" + _format_entry(entry))
    state["handled"][key] = entry_id
    state["next_id"] = int(state.get("next_id", 1)) + 1
    state["counters"][outcome] = int(state["counters"].get(outcome, 0)) + 1
    if outcome in POSTED_OUTCOMES and not imported:
        state["tick_posts"] = int(state.get("tick_posts", 0)) + 1
    clar = state["clarifications"]
    if outcome == "clarified":
        cur = clar.get(key, {"count": 0})
        clar[key] = {"id": entry_id, "asked_ts": entry["reply_ts"], "count": int(cur.get("count", 0)) + 1,
                     "resolved": False}
    elif key in clar:
        clar[key]["resolved"] = True
        clar[key]["resolved_by"] = entry_id
    save_state(target, state)
    return {"recorded": True, "id": entry_id, "outcome": outcome, "thread_key": key, "ledger": True}


def _grade_value(s: str) -> int:
    m = re.match(r"^\s*([+-]?\d)", str(s or ""))
    return int(m.group(1)) if m else None


def cmd_review(target: Path, args) -> dict:
    policy, _ = load_policy(target)
    state = load_state(target)
    text = _ledger_text(target)
    entries = parse_ledger(text)
    if args.action == "list":
        out = []
        for e in entries:
            if args.since and e["ts"] < args.since:
                continue
            if not args.all and e.get("grade"):
                continue
            out.append(e)
        return {"count": len(out), "entries": out}
    if args.action == "grade":
        if args.score not in ("1", "+1", "0", "-1"):
            raise Refusal("--score must be +1, 0 or -1")
        score = int(args.score)
        who = policy["owner_name"]
        text = _update_entry_field(text, args.id, "grade", f"{score:+d} ({_now().date().isoformat()}, {who})")
        if args.note:
            text = _update_entry_field(text, args.id, "note", args.note.replace("\n", " "))
        _write_atomic(respond_ledger_path(target), text)
        return {"graded": args.id, "score": score, "note": args.note or ""}
    if args.action == "reaction":
        if args.value not in ("+1", "1", "-1", "none"):
            raise Refusal("--value must be +1, -1 or none")
        val = "" if args.value == "none" else f"{int(args.value):+d} (asker)"
        text = _update_entry_field(text, args.id, "reaction", val)
        _write_atomic(respond_ledger_path(target), text)
        tightened_now = False
        if args.value == "-1" and window_open(state):
            state["negatives"] = int(state.get("negatives", 0)) + 1
            if state["negatives"] >= int(policy["tighten_after_negatives"]) and not state.get("tightened"):
                state["tightened"] = True
                tightened_now = True
            save_state(target, state)
        return {"reaction": args.id, "value": args.value, "negatives": state.get("negatives", 0),
                "tightened": state.get("tightened", False), "tightened_now": tightened_now}
    if args.action == "stats":
        counts = {o: 0 for o in ALL_OUTCOMES}
        graded = pos_owner = reacted = pos_asker = 0
        for e in entries:
            counts[e["outcome"]] = counts.get(e["outcome"], 0) + 1
            g = _grade_value(e.get("grade"))
            if g is not None:
                graded += 1
                pos_owner += 1 if g > 0 else 0
            r = _grade_value(e.get("reaction"))
            if r is not None:
                reacted += 1
                pos_asker += 1 if r > 0 else 0
        counts["ignored"] = int(state.get("counters", {}).get("ignored", 0))  # state only, never in the ledger
        answered = counts.get("answered", 0)
        negative_answered = [e["id"] for e in entries if e["outcome"] == "answered"
                             and (_grade_value(e.get("grade")) or 0) < 0]
        positive_answered = sum(1 for e in entries if e["outcome"] == "answered"
                                and (_grade_value(e.get("grade")) or 0) > 0)
        return {
            "entries": len(entries),
            "by_outcome": counts,
            "graded": graded,
            "owner_positive_share": round(pos_owner / graded, 2) if graded else None,
            "reacted": reacted,
            "asker_positive_share": round(pos_asker / reacted, 2) if reacted else None,
            "answered": answered,
            "answered_with_negative_grade": negative_answered,
            "unattended_bar": {"graded_needed": 20, "graded": graded,
                               "answered_graded_positive_needed": 5,
                               "answered_graded_positive": positive_answered,
                               "negative_answered": len(negative_answered),
                               "met": graded >= 20 and positive_answered >= 5 and not negative_answered,
                               "note": "needs graded answers, not only escalations; a replay of escalations alone never meets it"},
            "lessons": lessons_summary(target),
        }
    if args.action == "export":
        out = [e for e in entries if not args.graded_only or e.get("grade")]
        return {"count": len(out), "entries": out,
                "lessons_sections": list(LESSON_SECTIONS),
                "hint": "synthesize lessons.md from these; the owner approves each lesson's text"}
    raise Refusal(f"unknown review action {args.action!r}")


def _osascript_imessage(to: str, text: str) -> tuple:
    esc = text.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Messages"\n'
        "  set targetService to 1st account whose service type = iMessage\n"
        f'  set targetBuddy to participant "{to}" of targetService\n'
        f'  send "{esc}" to targetBuddy\n'
        "end tell"
    )
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                              timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def _twilio_sms(text: str) -> tuple:
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    tok = os.environ.get("TWILIO_AUTH_TOKEN")
    frm = os.environ.get("TWILIO_FROM")
    to = os.environ.get("TWILIO_TO")
    if not all((sid, tok, frm, to)):
        return False, "set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, TWILIO_TO"
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode({"From": frm, "To": to, "Body": text[:1500]}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed https host)
            return 200 <= resp.status < 300, f"http {resp.status}"
    except Exception as e:  # pylint: disable=broad-except
        return False, str(e)


def cmd_escalate(target: Path, args) -> dict:
    policy, _ = load_policy(target)
    text = args.text.strip()
    if not text:
        raise Refusal("--text is required")
    routes = _csv(args.route) or policy["escalation_routes"]
    results = []
    for route in routes:
        if args.dry_run:
            results.append({"route": route, "ok": True, "detail": "dry run; nothing sent", "text": text})
            continue
        if route == "push":
            results.append({"route": route, "ok": True, "detail": "handled by the model (PushNotification tool)"})
        elif route == "imessage":
            to = str(policy.get("imessage_to") or "")
            if not to:
                results.append({"route": route, "ok": False, "detail": "policy imessage_to is empty"})
                continue
            ok, detail = _osascript_imessage(to, text)
            results.append({"route": route, "ok": ok, "detail": detail})
        elif route == "twilio":
            ok, detail = _twilio_sms(text)
            results.append({"route": route, "ok": ok, "detail": detail})
        elif route == "slack":
            results.append({"route": route, "ok": True,
                            "detail": f"handled by the model (post in {policy.get('escalation_channel') or 'the escalation channel'})"})
        else:
            results.append({"route": route, "ok": False, "detail": f"unknown route {route!r}"})
    return {"routes": results, "all_ok": all(r["ok"] for r in results)}


# ---------- export (a self-contained kit for another agent) ----------

EXPORT_KIND = "compathy-respond-export"
EXPORT_SKILL = "compathy-respond"
EXPORT_PROJECT = "project"
EXPORT_SCRIPTS = ("respond.py", "compathy_query.py", "lint.py", "lineage.py", "paths.py")
EXPORT_FORBIDDEN = ("ledger.md", RESPOND_STATE_FILE, LOG_FILE)
EXPORT_TEMPLATES = (
    ("respond-export-SKILL.md", EXPORT_SKILL + "/SKILL.md"),
    ("respond-export-agent-instructions.md", "agent-instructions.md"),
    ("respond-export-schedule.md", "schedule-instructions.md"),
    ("respond-export-SETUP.md", "SETUP.md"),
)


def _filtered_index(index_text: str, keep: set) -> str:
    """The wiki index with only the kept pages' bullets, without its how-to-use tail."""
    out = []
    for line in index_text.splitlines():
        if line.strip().lower().startswith("## how to use"):
            break
        m = INDEX_BULLET_RE.match(line)
        if m and m.group(1).strip() not in keep:
            continue
        out.append(line)
    while out and out[-1].strip() in ("", "---"):
        out.pop()
    return "\n".join(out) + "\n"


def _set_frontmatter_key(text: str, key: str, value: str, after: str = "") -> str:
    """Set ``key: value`` in flat frontmatter, adding it after ``after`` when the key is missing."""
    pat = re.compile(rf"^{re.escape(key)}:.*$", re.M)
    if pat.search(text):
        return pat.sub(lambda _m: f"{key}: {value}", text, count=1)
    if after:
        m = re.search(rf"^{re.escape(after)}:.*$", text, re.M)
        if m:
            return text[:m.end()] + f"\n{key}: {value}" + text[m.end():]
    end = text.index("\n---", 3)
    return text[:end] + f"\n{key}: {value}" + text[end:]


def _strip_section(text: str, heading: str) -> str:
    """Drop a ``## heading`` section, up to the next ``## `` heading."""
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        if line.startswith("## "):
            skipping = line[3:].strip().lower().startswith(heading.lower())
        if not skipping:
            out.append(line)
    return "".join(out).rstrip("\n") + "\n"


def _links_valid_until(entries: list, max_age_days: int) -> str:
    """The first date a shared, connector-verified row stops being linkable (manual rows never do)."""
    dates = []
    for e in entries:
        if e["status"] != "shared" or e.get("via") == MANUAL_VIA:
            continue
        try:
            d = datetime.fromisoformat((e.get("verified") or "")[:10]).date()
        except ValueError:
            continue
        dates.append(d + timedelta(days=max_age_days))
    return min(dates).isoformat() if dates else ""


def _first_house_answer(target: Path) -> str:
    """The name of the first house answer in lessons.md, for the setup's test question."""
    p = respond_lessons_path(target)
    if not p.is_file():
        return ""
    fenced, section = False, ""
    for line in _read(p).splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if line.startswith("## "):
            section = line[3:].strip()
        elif line.startswith("### ") and section == "House answers":
            name = line[4:].strip()
            return name.split(":", 1)[1].strip() if ":" in name else name
    return ""


def cmd_export(target: Path, args) -> dict:
    """Build a kit another agent runs the stand-in from: the allowed pages, the rules, the gate."""
    policy, _ = load_policy(target)
    for key in ("owner_slack_id", "internal_domain"):
        if not policy.get(key):
            raise Refusal(f"policy.md has no {key}; the kit's check could not tell the owner or coworkers apart")
    max_age = int(args.link_max_age_days)
    if max_age < 1:
        raise Refusal("--link-max-age-days must be at least 1")
    out = Path(args.out).expanduser().resolve()
    ctx_root = context_root(target).resolve()
    if out == ctx_root or ctx_root in out.parents:
        raise Refusal("the kit cannot live inside context/; choose a folder outside it")
    if out.exists() and not out.is_dir():
        raise Refusal(f"{out} is a file")
    if out.exists() and any(out.iterdir()):
        try:
            earlier = json.loads(_read(out / "MANIFEST.json")).get("kind") == EXPORT_KIND
        except (OSError, ValueError, AttributeError):
            earlier = False
        if not earlier:
            raise Refusal(f"{out} is not empty and holds no earlier export; choose an empty folder")
        if not args.force:
            raise Refusal(f"{out} holds an earlier export; pass --force to replace it")
        shutil.rmtree(out)

    corpus = compute_corpus(target, policy)
    pages = corpus["pages"]
    if not pages:
        raise Refusal("the corpus is empty; nothing to export")

    skill = out / EXPORT_SKILL
    project = skill / EXPORT_PROJECT
    wiki_out = project / "context" / "wiki"
    resp_out = project / "context" / "respond"
    try:
        for d in (skill / "scripts", wiki_out, resp_out):
            d.mkdir(parents=True, exist_ok=True)
        here = Path(__file__).resolve().parent
        for name in EXPORT_SCRIPTS:
            shutil.copy2(here / name, skill / "scripts" / name)
        keep, copied = set(), {}
        for rec in pages:
            src = Path(rec["path"])
            src = src if src.is_absolute() else target / src
            sub = src.parent.name if src.parent.name in WIKI_SUBDIRS else "concepts"
            (wiki_out / sub).mkdir(exist_ok=True)
            shutil.copy2(src, wiki_out / sub / f"{rec['slug']}.md")
            keep.add(rec["slug"])
            copied[rec["slug"]] = wiki_out / sub / f"{rec['slug']}.md"
        idx = wiki_dir(target) / INDEX_FILE
        index_text = _read(idx) if idx.is_file() else "---\ntype: index\nschema_version: 1\n---\n\n# Index\n"
        _write_atomic(wiki_out / INDEX_FILE, _filtered_index(index_text, keep))

        owner = str(policy["owner_name"])
        log_channel = args.log_channel or str(policy.get("escalation_channel") or "") or (
            "#" + re.sub(r"[^a-z0-9]+", "-", owner.lower()).strip("-") + "-standin-log")
        pol = _read(respond_policy_path(target))
        pol = _set_frontmatter_key(pol, "link_verify_max_age_days", str(max_age), after="link_audiences")
        pol = _set_frontmatter_key(pol, "escalation_routes", "[slack-reminder]")
        pol = _set_frontmatter_key(pol, "escalation_channel", f'"{log_channel}"')
        pol = _strip_section(pol, "Review checklist")
        _write_atomic(resp_out / "policy.md", pol)
        shutil.copy2(respond_sources_path(target), resp_out / "sources.md")
        shutil.copy2(respond_lessons_path(target), resp_out / "lessons.md")

        # files/: the same knowledge as four plain files for an agent builder's Files section.
        files_dir = out / "files"
        files_dir.mkdir(exist_ok=True)
        index_body = cq._safe_frontmatter(_read(wiki_out / INDEX_FILE))[1].strip()  # pylint: disable=protected-access
        type_order = {"concept": 0, "entity": 1, "summary": 2, "patterns": 3}
        parts = [f"# {args.project_name or target.name} wiki, exported {_now().date().isoformat()}\n\n"
                 "The only pages the stand-in may answer from. The index comes first; each page starts "
                 "with a line like <!-- page: slug | type | updated: date -->.\n\n", index_body, "\n"]
        for rec in sorted(pages, key=lambda r: (type_order.get(r["type"], 9), r["slug"])):
            fm, body = cq._safe_frontmatter(_read(copied[rec["slug"]]))  # pylint: disable=protected-access
            parts.append(f"\n<!-- page: {rec['slug']} | {rec['type']} | updated: {fm.get('updated', '')} -->"
                         f"\n\n{body.strip()}\n")
        _write_atomic(files_dir / "wiki.md", "".join(parts))
        for name in ("policy.md", "lessons.md", "sources.md"):
            shutil.copy2(resp_out / name, files_dir / name)

        # Nothing private rides along, and the kit's own check sees exactly the exported pages.
        problems = []
        excluded = {e["slug"] for e in corpus["excluded"]}
        for p in out.rglob("*"):
            if p.name in EXPORT_FORBIDDEN or p.name.startswith("handoff-") or (p.is_dir() and p.name == "raw"):
                problems.append(f"private file in the kit: {p.relative_to(out)}")
            if p.suffix == ".md" and p.parent.parent == wiki_out and p.stem in excluded:
                problems.append(f"excluded page in the kit: {p.stem}")
        wiki_text = _read(files_dir / "wiki.md")
        for slug in sorted(excluded):
            if f"<!-- page: {slug} |" in wiki_text:
                problems.append(f"excluded page in files/wiki.md: {slug}")
        saved_layers = list(cq.EXTRA_LAYERS)
        cq.EXTRA_LAYERS = []
        try:
            kit_policy, _ = load_policy(project)
            kit_corpus = compute_corpus(project, kit_policy)
        finally:
            cq.EXTRA_LAYERS = saved_layers
        got = {p["slug"] for p in kit_corpus["pages"]}
        if got != keep or kit_corpus["excluded"]:
            problems.append(f"the kit's corpus differs from the export ({len(got)} allowed, "
                            f"{len(kit_corpus['excluded'])} excluded, {len(keep)} exported)")
        if problems:
            raise Refusal("export stopped: " + "; ".join(problems))

        kit_entries, _ = load_registry(project)
        shared = shareable(kit_entries, kit_policy)
        valid_until = _links_valid_until(kit_entries, max_age)
        tests = kit_policy["test_channels"]
        ctx = {
            "owner_name": owner,
            "owner_slack_id": policy["owner_slack_id"],
            "internal_domain": policy["internal_domain"],
            "header": policy["header"],
            "footer": policy["footer"],
            "project_name": args.project_name or target.name,
            "date": _now().date().isoformat(),
            "pages": len(keep),
            "shared_links": len(shared),
            "links_valid_until": valid_until or "your next export",
            "link_max_age_days": max_age,
            "log_channel": log_channel,
            "test_channel": args.test_channel_name or (
                "the test channel in policy.md (" + ", ".join(tests) + ")" if tests
                else "a private test channel (add its ID to test_channels in policy.md and export again)"),
            "max_posts": kit_policy["max_posts_per_tick"],
            "max_lines": kit_policy["max_reply_lines"],
            "max_chars": kit_policy["max_reply_chars"],
            "first_house_answer": _first_house_answer(target) or "a question a house answer covers",
            "export_command": f'python3 "{Path(__file__).resolve()}" --target "{target}" export --out "{out}" --force',
            "example_return": (_now() + timedelta(days=7)).strftime("%Y-%m-%d") + " 09:00",
        }
        for tpl, rel in EXPORT_TEMPLATES:
            text = _render(tpl, ctx)
            if "{{" in text:
                raise Refusal(f"template {tpl} left a placeholder unfilled")
            _write_atomic(out / rel, text)
        archive = Path(shutil.make_archive(str(out / EXPORT_SKILL), "zip", root_dir=out, base_dir=EXPORT_SKILL))
        files = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
        manifest = {
            "kind": EXPORT_KIND,
            "version": 1,
            "exported_at": _iso(_now()),
            "source": str(target),
            "pages": sorted(keep),
            "excluded_count": len(excluded),
            "shared_links": len(shared),
            "link_max_age_days": max_age,
            "links_valid_until": valid_until,
            "log_channel": log_channel,
            "zip": archive.name,
            "zip_bytes": archive.stat().st_size,
            "files": files,
        }
        _write_atomic(out / "MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    return {
        "kit": str(out),
        "setup": str(out / "SETUP.md"),
        "instructions": str(out / "agent-instructions.md"),
        "files_dir": str(out / "files"),
        "zip": str(archive),
        "zip_bytes": archive.stat().st_size,
        "pages": len(keep),
        "excluded": len(excluded),
        "shared_links": len(shared),
        "links_valid_until": valid_until,
        "log_channel": log_channel,
        "files": len(files) + 1,
    }


# ---------- CLI ----------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Bookkeeping and guardrails for /compathy-respond")
    ap.add_argument("--target", default=".", help="project root (contains context/)")
    ap.add_argument("--layer", action="append", default=[],
                    help="extra context/ or wiki/ dir to read as an ad-hoc layer (repeatable)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("--project-name", default="")
    p.add_argument("--owner-name", default="")
    p.add_argument("--owner-slack-id", default="")
    p.add_argument("--internal-domain", default="")
    p.add_argument("--roadmap-page", default="")
    p.add_argument("--force", action="store_true",
                   help="rewrite policy, sources and lessons from the templates (ledger and state are kept)")

    sub.add_parser("corpus")

    p = sub.add_parser("search")
    p.add_argument("--query", action="append", required=True)
    p.add_argument("--max-results", type=int, default=10)

    p = sub.add_parser("page")
    p.add_argument("--slug", required=True)

    p = sub.add_parser("sources")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "seed", "apply-rules", "set", "verdict"])
    p.add_argument("--match", default="")
    p.add_argument("--status", default="")
    p.add_argument("--needs-verification", action="store_true")
    p.add_argument("--url", default="")
    p.add_argument("--kind", default="")
    p.add_argument("--audience", default="")
    p.add_argument("--via", default="")
    p.add_argument("--title", default="")
    p.add_argument("--note", default="")
    p.add_argument("--json", default="{}")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("window")
    p.add_argument("action", choices=["start", "status", "stop", "update"])
    p.add_argument("--until")
    p.add_argument("--every")
    p.add_argument("--mode")
    p.add_argument("--scope")
    p.add_argument("--channels")
    p.add_argument("--route")
    p.add_argument("--task-id")

    p = sub.add_parser("tick")
    p.add_argument("action", choices=["begin", "end"])
    p.add_argument("--since", help="replay: look back this far (7d, 24h) instead of the last tick")

    p = sub.add_parser("check")
    p.add_argument("--json", required=True)
    p.add_argument("--offline", action="store_true", help="skip window and cap checks (tests, replay)")
    p.add_argument("--follow-up", action="store_true")

    p = sub.add_parser("record")
    p.add_argument("--json", required=True)
    p.add_argument("--offline", action="store_true")
    p.add_argument("--follow-up", action="store_true")
    p.add_argument("--imported", action="store_true",
                   help="a line from a remote stand-in's log: record it as it happened, without the gate")

    p = sub.add_parser("export")
    p.add_argument("--out", required=True, help="an empty folder, or an earlier export with --force")
    p.add_argument("--force", action="store_true", help="replace an earlier export in --out")
    p.add_argument("--link-max-age-days", type=int, default=30,
                   help="how long the kit treats a verified link as shareable (default 30)")
    p.add_argument("--log-channel", default="", help="the private Slack channel for log lines")
    p.add_argument("--test-channel-name", default="", help="how SETUP.md names the test channel")
    p.add_argument("--project-name", default="")

    p = sub.add_parser("review")
    p.add_argument("action", choices=["list", "grade", "reaction", "stats", "export"])
    p.add_argument("--id")
    p.add_argument("--score")
    p.add_argument("--value")
    p.add_argument("--note", default="")
    p.add_argument("--since", default="")
    p.add_argument("--all", action="store_true")
    p.add_argument("--graded-only", action="store_true")

    p = sub.add_parser("escalate")
    p.add_argument("--text", required=True)
    p.add_argument("--route", default="")
    p.add_argument("--dry-run", action="store_true")
    return ap


COMMANDS = {
    "init": cmd_init,
    "corpus": cmd_corpus,
    "search": cmd_search,
    "page": cmd_page,
    "sources": cmd_sources,
    "window": cmd_window,
    "tick": cmd_tick,
    "check": cmd_check,
    "record": cmd_record,
    "review": cmd_review,
    "escalate": cmd_escalate,
    "export": cmd_export,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    target = Path(args.target).resolve()
    cq.EXTRA_LAYERS = list(args.layer)
    try:
        if args.cmd != "init":
            _require_respond(target)
        result = COMMANDS[args.cmd](target, args)
    except Refusal as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        return 1
    except cq.ToolError as e:
        print(json.dumps({"ok": False, "error": e.message}, indent=2))
        return 1
    code = int(result.pop("exit", 0)) if isinstance(result, dict) else 0
    if isinstance(result, dict) and "ok" in result and result["ok"] is False and code == 0:
        code = 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
