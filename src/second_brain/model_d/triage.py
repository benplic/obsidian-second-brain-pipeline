"""Interactive triage for inbox and manual-review notes.

Commands are single letters. Frontmatter changes go through ``rebuild_card``
so the note body is preserved. Moves write the destination atomically and
only then delete the source. A destination that already exists gets a
``(2)`` suffix instead of being overwritten.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..io_utils import atomic_write_text
from ..ledger import EVENT_TOSSED, UrlLedger
from ..taxonomy import category_tag, folder_for
from ..vault import (
    parse_frontmatter,
    parse_frontmatter_strict,
    rebuild_card,
    sanitize_filename,
    split_frontmatter,
    unique_card_path,
)

logger = logging.getLogger(__name__)

# P.A.R.A. project root used by the sample vault and the Model D spec.
PROJECTS_DIRNAME = "1 - Projects"

_SKIP_SUFFIXES = ("Hub.md", "Kanban.md", "Map.md")
_MANUAL_REVIEW_PARTS = ("Inbox", "Manual Review")
# Human triage outcomes. A note in one of these states is finished even when
# it still sits in Inbox/Manual Review or still carries stage: manual-review.
_TERMINAL_STATUSES = {"kept", "promoted", "tossed"}
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/*?:"<>|]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_COMMAND_PROMPT = "[k]eep  [m]ove  [p]romote  [t]oss  [s]kip  [q]uit: "
_TOSS_PROMPT = "Hide with status tossed, or delete the file? [Enter/t] toss  [d] delete  [c] cancel: "
_FOLDER_PROMPT = "Target folder under 3 - Resources (empty cancels): "
_PROJECT_PROMPT = "Project name (empty cancels): "

Ask = Callable[[str], str]


@dataclass(frozen=True)
class Candidate:
    path: Path
    content: str
    fm: dict | None
    rank: int


@dataclass
class TriageCounts:
    kept: int = 0
    moved: int = 0
    promoted: int = 0
    tossed: int = 0
    deleted: int = 0
    skipped: int = 0


def category_for_folder(folder: str, folder_map: dict[str, str]) -> str:
    """Taxonomy key that files into ``folder``, or the folder name.

    ``Comedy`` maps back to ``Comedy/Funny``. A folder with no key, or with
    more than one, keeps the folder name so we do not guess.
    """
    matches = [key for key, dest in folder_map.items() if dest == folder]
    if folder in matches:
        return folder
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        logger.warning(
            "Folder %s matches several categories %s; using the folder name.",
            folder,
            matches,
        )
    return folder


def replace_category_tag(tags: object, category: str) -> list:
    """Drop existing ``category/*`` tags and append the tag for ``category``."""
    kept: list = []
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, str) and tag.startswith("category/"):
                continue
            kept.append(tag)
    new_tag = category_tag(category)
    if new_tag not in kept:
        kept.append(new_tag)
    return kept


def clean_folder_segment(raw: str) -> str | None:
    """One vault folder name. Reject path separators and illegal characters.

    Unlike ``sanitize_filename``, this does not silently strip characters.
    A typo should fail the prompt instead of creating a near-miss folder.
    """
    name = " ".join((raw or "").split())
    if not name or name in {".", ".."} or name.startswith("."):
        return None
    if "/" in name or "\\" in name or _ILLEGAL_FILENAME_CHARS.search(name):
        return None
    if name.rstrip(". ") != name:
        return None
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        return None
    return name


def existing_directory_name(parent: Path, name: str) -> str:
    """Return the on-disk folder name when it differs only by case.

    ``Comedy`` and a typed ``comedy`` are the same directory on Windows, but
    the taxonomy map is case-sensitive. Prefer the exact entry, then the only
    case-insensitive match, so the category key stays ``Comedy/Funny``.
    """
    if not parent.is_dir():
        return name
    matches = [
        child.name
        for child in parent.iterdir()
        if child.is_dir() and child.name.casefold() == name.casefold()
    ]
    if name in matches or len(matches) != 1:
        return name
    return matches[0]


def clean_project_name(raw: str) -> str | None:
    """Project folder name with illegal characters removed.

    Slashes are rejected up front so ``../`` cannot be sanitized into a
    different-looking name that still escapes the projects directory.
    """
    text = raw or ""
    if "/" in text or "\\" in text:
        return None
    name = " ".join(text.split())
    if not name or name in {".", ".."}:
        return None
    cleaned = sanitize_filename(name, max_len=80, fallback="")
    if not cleaned or cleaned in {".", ".."} or cleaned.startswith("."):
        return None
    return cleaned


def _contained(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _field(fm: dict | None, key: str) -> str:
    if not fm:
        return ""
    value = fm.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _can_rewrite(candidate: Candidate) -> bool:
    if candidate.fm is None:
        return False
    return split_frontmatter(candidate.content)[0] is not None


def _rank(path: Path, fm: dict | None, resources_dir: Path) -> int:
    try:
        parts = path.relative_to(resources_dir).parts
    except ValueError:
        parts = path.parts
    status = _field(fm, "status").lower()
    stage = _field(fm, "stage").lower()
    if parts[:2] == _MANUAL_REVIEW_PARTS:
        return 0
    if status == "manual-review" or stage == "manual-review":
        return 1
    return 2


def _in_manual_review(path: Path, resources_dir: Path) -> bool:
    try:
        return path.relative_to(resources_dir).parts[:2] == _MANUAL_REVIEW_PARTS
    except ValueError:
        return False


def _is_queued(path: Path, fm: dict | None, resources_dir: Path) -> bool:
    if path.name.startswith(".") or path.name.endswith(_SKIP_SUFFIXES):
        return False
    try:
        path.relative_to(resources_dir)
    except ValueError:
        return False
    # Checked before the holding-folder rule. Otherwise keep/toss leaves the
    # note in Inbox/Manual Review and the next run shows it again.
    status = _field(fm, "status").lower()
    if status in _TERMINAL_STATUSES:
        return False
    if _in_manual_review(path, resources_dir):
        return True
    stage = _field(fm, "stage").lower()
    return status in {"inbox", "manual-review"} or stage == "manual-review"


def _clear_manual_review_stage(fm: dict) -> None:
    """Drop pipeline manual-review. Other stages (ready, to-watch) stay."""
    if _field(fm, "stage").lower() == "manual-review":
        fm.pop("stage", None)


_STAGE_LINE_RE = re.compile(r"^stage:\s*[\"']?([^\"'\r\n]+)[\"']?\s*$", re.MULTILINE)


def _queue_frontmatter(content: str, strict: dict | None) -> dict:
    """Frontmatter for queue and rank only.

    Strict YAML is used when it parses. A broken block falls back to the
    url/status regex, plus a stage line, because manual-review lives in
    ``stage`` while ``status`` stays ``inbox``. That recovered dict is never
    passed to ``rebuild_card``.
    """
    if strict is not None:
        return strict
    recovered = dict(parse_frontmatter(content))
    fm_text, _ = split_frontmatter(content)
    if fm_text and (match := _STAGE_LINE_RE.search(fm_text)):
        recovered["stage"] = match.group(1).strip()
    return recovered


def collect_candidates(resources_dir: Path) -> list[Candidate]:
    """Manual Review first, then other manual-review notes, then inbox."""
    resources_dir = Path(resources_dir)
    found: list[Candidate] = []
    if not resources_dir.is_dir():
        return found
    for root, dirs, files in os.walk(resources_dir):
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        for name in sorted(files):
            if not name.endswith(".md") or name.startswith("."):
                continue
            path = Path(root) / name
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("Skipping unreadable note %s: %s", path, exc)
                continue
            strict = parse_frontmatter_strict(content)
            # A YAML error is None, which looks like "no status" and would
            # hide a broken inbox card or re-queue a tossed holding-folder
            # note. Queue decisions use a lenient read of status and stage.
            # Rewrites keep the strict dict so rebuild_card cannot drop every key.
            queue_fm = _queue_frontmatter(content, strict)
            if not _is_queued(path, queue_fm, resources_dir):
                continue
            if strict is None:
                logger.warning("Unparseable frontmatter in %s", path)
            found.append(Candidate(
                path=path,
                content=content,
                fm=strict,
                rank=_rank(path, queue_fm, resources_dir),
            ))
    found.sort(key=lambda item: (item.rank, str(item.path).lower()))
    return found


def _excerpt(body: str, limit: int = 280) -> str:
    match = re.search(r"^## Notes[^\n]*\n(.*)", body, re.MULTILINE | re.DOTALL)
    text = match.group(1).strip() if match else body.strip()
    text = " ".join(text.split())
    if not text:
        return "(no notes)"
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def _title(path: Path, content: str) -> str:
    _, body = split_frontmatter(content)
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            if title:
                return title
    return path.stem


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _render_card(index: int, total: int, candidate: Candidate, vault_path: Path) -> str:
    _, body = split_frontmatter(candidate.content)
    # Display can use the lenient read. Rewrites still require candidate.fm.
    fm = candidate.fm if candidate.fm is not None else _queue_frontmatter(candidate.content, None)
    tags = fm.get("tags")
    if isinstance(tags, list):
        tag_text = ", ".join(str(tag) for tag in tags) or "(none)"
    else:
        tag_text = "(none)"
    url = _field(fm, "url") or "(none)"
    return "\n".join([
        f"[{index}/{total}] {_title(candidate.path, candidate.content)}",
        f"  path: {_relative(candidate.path, vault_path)}",
        f"  url: {url}",
        f"  category: {_field(fm, 'category') or '(none)'}",
        f"  summary: {_field(fm, 'summary') or '(none)'}",
        f"  tags: {tag_text}",
        f"  notes: {_excerpt(body)}",
    ])


def _rewrite(candidate: Candidate, fm: dict, dest_dir: Path, root: Path) -> Path:
    """Write updated frontmatter under ``root`` and remove the source if it moved."""
    if not _contained(dest_dir, root):
        raise ValueError(f"Refusing to write outside {root}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not _contained(dest_dir, root):
        raise ValueError(f"Refusing to write outside {root}")
    new_content = rebuild_card(candidate.content, fm)
    if candidate.path.parent.resolve() == dest_dir.resolve():
        atomic_write_text(candidate.path, new_content)
        return candidate.path
    dest = unique_card_path(dest_dir, candidate.path.stem)
    if not _contained(dest, root):
        raise ValueError(f"Refusing to write outside {root}")
    atomic_write_text(dest, new_content)
    try:
        candidate.path.unlink()
    except OSError as exc:
        logger.error(
            "Wrote %s but could not remove %s (%s). Both copies exist; delete one after checking them.",
            dest,
            candidate.path,
            exc,
        )
        raise
    return dest


def _apply_category(fm: dict, folder: str, folder_map: dict[str, str]) -> None:
    category = category_for_folder(folder, folder_map)
    fm["category"] = category
    fm["tags"] = replace_category_tag(fm.get("tags"), category)


def _note_url(candidate: Candidate) -> str:
    """URL from strict frontmatter, or the lenient regex when YAML is broken.

    A strict failure drops every key. The ledger still needs the url line so
    a deleted card is not fetched and billed again.
    """
    url = _field(candidate.fm, "url")
    if url:
        return url
    return _field(parse_frontmatter(candidate.content), "url")


def _keep_directory(candidate: Candidate, settings: Settings) -> Path:
    """Where a kept note belongs.

    A note already in a category folder stays there. A note in the holding
    folder is filed with ``folder_for``, because Inbox/Manual Review is not
    a category hub and would never list it.
    """
    if not _in_manual_review(candidate.path, settings.resources_dir):
        return candidate.path.parent
    folder = folder_for(_field(candidate.fm, "category"), settings.folder_map)
    folder = existing_directory_name(settings.resources_dir, folder)
    return settings.resources_dir / folder


def _delete_note(settings: Settings, candidate: Candidate) -> None:
    url = _note_url(candidate)
    if url:
        # Record the toss before unlinking so a crash cannot drop the URL
        # and cause the next ingest to spend another Gemini request on it.
        UrlLedger(settings.ledger_path).record(url, EVENT_TOSSED, source="model-d")
    else:
        logger.warning("Deleting %s with no URL; it cannot be recorded in the ledger.", candidate.path)
    try:
        candidate.path.unlink()
    except OSError as exc:
        logger.error("Could not delete %s: %s", candidate.path, exc)
        raise


def _prompt_action(candidate: Candidate, settings: Settings, ask: Ask, counts: TriageCounts) -> bool:
    """Handle one note. Return True when the session should stop."""
    if not _can_rewrite(candidate):
        print("This note's frontmatter could not be parsed. Skip, quit, or toss and then confirm delete.")
    while True:
        try:
            choice = ask(_COMMAND_PROMPT).strip().lower()
        except EOFError:
            print()
            return True
        if choice in {"q", "quit"}:
            return True
        if choice in {"s", "skip"}:
            counts.skipped += 1
            return False
        if not _can_rewrite(candidate) and choice not in {"t", "toss"}:
            print("Choose [s]kip, [q]uit, or [t]oss, then d to delete.")
            continue
        if choice in {"k", "keep"}:
            updated = dict(candidate.fm or {})
            updated["status"] = "kept"
            _clear_manual_review_stage(updated)
            dest_dir = _keep_directory(candidate, settings)
            dest = _rewrite(candidate, updated, dest_dir, settings.resources_dir)
            counts.kept += 1
            print(f"Kept {_relative(dest, settings.vault_path)}")
            return False
        if choice in {"m", "move"}:
            folder_raw = ask(_FOLDER_PROMPT).strip()
            if not folder_raw:
                print("Move cancelled.")
                continue
            folder_name = clean_folder_segment(folder_raw)
            if folder_name is None:
                print("Not a valid folder name. Use a single folder, with no slashes.")
                continue
            folder_name = existing_directory_name(settings.resources_dir, folder_name)
            dest_dir = settings.resources_dir / folder_name
            if dest_dir.exists() and not dest_dir.is_dir():
                print(f"{folder_name} exists and is not a folder.")
                continue
            if not _contained(dest_dir, settings.resources_dir):
                print("That folder is outside 3 - Resources.")
                continue
            updated = dict(candidate.fm or {})
            _apply_category(updated, folder_name, settings.folder_map)
            _clear_manual_review_stage(updated)
            dest = _rewrite(candidate, updated, dest_dir, settings.resources_dir)
            counts.moved += 1
            print(f"Moved to {_relative(dest, settings.vault_path)}")
            return False
        if choice in {"p", "promote"}:
            project_raw = ask(_PROJECT_PROMPT)
            if not project_raw.strip():
                print("Promote cancelled.")
                continue
            project_name = clean_project_name(project_raw)
            if project_name is None:
                print("Not a valid project name.")
                continue
            projects_root = settings.vault_path / PROJECTS_DIRNAME
            project_name = existing_directory_name(projects_root, project_name)
            if project_name != " ".join(project_raw.split()):
                print(f"Project folder: {project_name}")
            dest_dir = projects_root / project_name
            if not _contained(dest_dir, projects_root):
                print("That project name is outside 1 - Projects.")
                continue
            updated = dict(candidate.fm or {})
            updated["status"] = "promoted"
            _clear_manual_review_stage(updated)
            dest = _rewrite(candidate, updated, dest_dir, projects_root)
            counts.promoted += 1
            print(f"Promoted to {_relative(dest, settings.vault_path)}")
            return False
        if choice in {"t", "toss"}:
            try:
                decision = ask(_TOSS_PROMPT).strip().lower()
            except EOFError:
                print()
                return True
            if decision in {"c", "cancel"}:
                print("Toss cancelled.")
                continue
            if decision in {"d", "delete"}:
                _delete_note(settings, candidate)
                counts.deleted += 1
                print(f"Deleted {candidate.path.name}")
                return False
            if not _can_rewrite(candidate):
                print("Status cannot be updated until the frontmatter parses. Press d to delete, or c to cancel.")
                continue
            if decision in {"", "t", "toss"}:
                updated = dict(candidate.fm or {})
                updated["status"] = "tossed"
                _clear_manual_review_stage(updated)
                _rewrite(candidate, updated, candidate.path.parent, settings.resources_dir)
                counts.tossed += 1
                print(
                    "Set status to tossed. The file is still in the vault. "
                    "Run second-brain ledger-sync before you delete it."
                )
                return False
            print("Toss cancelled.")
            continue
        print("Unknown command. Use k, m, p, t, s, or q.")


def _default_ask(prompt: str) -> str:
    return input(prompt)


def run_triage(settings: Settings, *, ask: Ask | None = None) -> int:
    """Walk queued notes. Return 0, or 2 when there is no interactive terminal.

    Pass ``ask`` from tests. Without it, a non-TTY stdin exits instead of
    blocking on ``input``.
    """
    if ask is None:
        if not sys.stdin.isatty():
            logger.error("model-d --triage needs an interactive terminal.")
            return 2
        ask = _default_ask

    settings.require_vault()
    candidates = collect_candidates(settings.resources_dir)
    if not candidates:
        print("No inbox or manual-review notes.")
        return 0

    counts = TriageCounts()
    total = len(candidates)
    for index, candidate in enumerate(candidates, start=1):
        print(_render_card(index, total, candidate, settings.vault_path))
        try:
            if _prompt_action(candidate, settings, ask, counts):
                break
        except EOFError:
            print()
            break
        except ValueError as exc:
            # A rejected path or a note with no frontmatter block. Keep going
            # so one bad card does not abandon the rest of the queue.
            logger.error("%s", exc)
            counts.skipped += 1
        except OSError as exc:
            logger.error("Could not update %s: %s", candidate.path, exc)
            counts.skipped += 1
    print(
        "Triage done: "
        f"{counts.kept} kept, {counts.moved} moved, {counts.promoted} promoted, "
        f"{counts.tossed} tossed, {counts.deleted} deleted, {counts.skipped} skipped."
    )
    logger.info(
        "Triage done: kept=%d moved=%d promoted=%d tossed=%d deleted=%d skipped=%d",
        counts.kept,
        counts.moved,
        counts.promoted,
        counts.tossed,
        counts.deleted,
        counts.skipped,
    )
    return 0
