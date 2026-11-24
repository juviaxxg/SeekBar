import heapq
import os
import re
import select
import subprocess
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Result:
    path: str
    kind: str

    @property
    def name(self):
        name = os.path.basename(self.path) or self.path
        return name[:-4] if self.kind == "app" else name


def normalize(text):
    return "".join(
        char for char in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(char)
    )


def classify(path):
    if path.lower().endswith(".app") and os.path.isdir(path):
        return Result(path, "app")
    return Result(path, "folder" if os.path.isdir(path) else "file")


def is_navigable_folder(path):
    if not path or not os.path.isdir(path):
        return False
    paths = (Path(path), Path(os.path.realpath(path)))
    return not any(part.casefold().endswith(".app") for candidate in paths for part in candidate.parts)


def matches_scope(result, scope):
    if scope == "folder":
        return result.kind == "folder" and is_navigable_folder(result.path)
    return scope == "all" or result.kind == scope


def discover_apps():
    roots = ("/Applications", "/System/Applications", "/System/Library/CoreServices/Applications", "~/Applications")
    for root in roots:
        for folder, children, _ in os.walk(os.path.expanduser(root)):
            for child in list(children):
                if child.lower().endswith(".app"):
                    children.remove(child)
                    yield Result(os.path.join(folder, child), "app")
                elif child.startswith(".") or Path(folder).relative_to(os.path.expanduser(root)).parts:
                    children.remove(child)


def name_score(query, name):
    name = normalize(name)
    if query == name:
        return 120
    if name.startswith(query):
        return 100
    words = query.split()
    if all(word in name for word in words):
        return 75 if query in name else 65
    if len(words) != 1 or not query.isalnum():
        return 0
    if re.search(".*?".join(map(re.escape, query)), name):
        return 38
    return 0


def spotlight_literal(text):
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("*", "\\*").replace("?", "\\?")


def spotlight_paths(predicate, root=None, limit=400, timeout=1.0):
    command = ["/usr/bin/mdfind", "-0"]
    if root:
        command.extend(("-onlyin", root))
    command.append(predicate)
    paths = []
    pending = b""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            while len(paths) < limit:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                    break
                chunk = os.read(process.stdout.fileno(), 16384)
                if not chunk:
                    break
                parts = (pending + chunk).split(b"\0")
                pending = parts.pop()
                paths.extend(os.fsdecode(part) for part in parts if part)
        finally:
            if process.poll() is None:
                process.kill()
    return paths[:limit]


def exact_name(query, result):
    name = normalize(result.name)
    return query == name or (result.kind == "file" and query == os.path.splitext(name)[0])


def local_matches(roots, query, scope, suffix, inaccessible):
    words = query.split()
    for root in roots:
        found = []
        folders = deque([root])
        deadline = time.monotonic() + 0.30
        remaining = 8000
        while folders and remaining and time.monotonic() < deadline:
            folder = folders.popleft()
            try:
                with os.scandir(folder) as entries:
                    # The first access can wait for the user's macOS permission decision.
                    if folder == root:
                        deadline = time.monotonic() + 0.30
                    for entry in entries:
                        if not remaining or time.monotonic() >= deadline:
                            break
                        remaining -= 1
                        if entry.name.startswith("."):
                            continue
                        try:
                            is_folder = entry.is_dir(follow_symlinks=False)
                            is_app = is_folder and entry.name.lower().endswith(".app")
                            if is_folder and not is_app:
                                folders.append(entry.path)
                            if entry.is_symlink():
                                is_folder = entry.is_dir()
                                is_app = is_folder and entry.name.lower().endswith(".app")
                        except OSError:
                            continue
                        kind = "app" if is_app else "folder" if is_folder else "file"
                        if (scope != "all" and scope != kind) or (suffix and not entry.name.casefold().endswith(suffix)):
                            continue
                        result = Result(entry.path, kind)
                        name = normalize(result.name)
                        if all(word in name for word in words):
                            found.append(result)
            except PermissionError:
                if folder == root and inaccessible is not None:
                    inaccessible.add(root)
            except OSError:
                continue
        yield from found
        if any(exact_name(query, result) for result in found):
            break


def search(query, scope, root, apps, preferred, recent, limit=80, *, local_roots=(), inaccessible=None):
    query = query.strip()
    if not query:
        candidates = list(preferred) + list(recent) + list(apps[:18])
        results = dict.fromkeys(result for result in candidates if matches_scope(result, scope))
        return list(results)[:limit]

    if query.startswith(("/", "~/")):
        path = os.path.abspath(os.path.expanduser(query))
        if os.path.exists(path):
            result = classify(path)
            return [result] if matches_scope(result, scope) else []
        return []

    query = query.replace("\x00", "").strip()
    if not query:
        return []
    clean_query = normalize(query)
    candidates = {result.path: result for result in (*preferred, *recent)}
    if scope in ("all", "app"):
        candidates.update((result.path, result) for result in apps)
    if scope != "app":
        extension = re.fullmatch(r"(.+?)\s+ext:([\w.-]+)", query, flags=re.IGNORECASE)
        terms = extension.group(1) if extension else query
        clean_query = normalize(terms)
        predicates = [f'kMDItemFSName == "*{spotlight_literal(term)}*"cd' for term in terms.split()]
        conditions = []
        if extension:
            conditions.append(f'kMDItemFSName == "*.{spotlight_literal(extension.group(2).lstrip("."))}"cd')
        if scope == "folder":
            conditions.append('kMDItemContentType == "public.folder"')
        else:
            conditions.append('kMDItemContentType != "com.apple.application-bundle"')
        suffix = "." + extension.group(2).lstrip(".").casefold() if extension else None
        paths = spotlight_paths(" && ".join(predicates + conditions), root=root)
        for path in paths:
            if path not in candidates and os.path.exists(path):
                candidates[path] = classify(path)

        def matches_exact(result):
            in_scope = matches_scope(result, scope)
            within_root = not root or result.path == root or result.path.startswith(root.rstrip("/") + "/")
            return (
                in_scope and within_root and (not suffix or result.path.casefold().endswith(suffix))
                and exact_name(clean_query, result)
            )

        if len(clean_query) >= 2 and not any(map(matches_exact, candidates.values())):
            for result in local_matches((root,) if root else local_roots, clean_query, scope, suffix, inaccessible):
                candidates[result.path] = result

        if len(paths) < 18 and not any(map(matches_exact, candidates.values())) and 3 <= len(terms) <= 15 and terms.isalnum():
            pattern = "*".join(map(spotlight_literal, terms))
            fuzzy = [f'kMDItemFSName == "*{pattern}*"cd', *conditions]
            for path in spotlight_paths(" && ".join(fuzzy), root=root, limit=180, timeout=0.6):
                if path not in candidates and os.path.exists(path):
                    candidates[path] = classify(path)
        if suffix:
            candidates = {path: result for path, result in candidates.items() if path.casefold().endswith(suffix)}
        if root:
            candidates = {
                path: result for path, result in candidates.items()
                if result.kind == "app" or path == root or path.startswith(root.rstrip("/") + "/")
            }

    recent_ranks = {result.path: max(1, 14 - index) for index, result in enumerate(recent)}
    favorite_paths = {result.path for result in preferred}
    ranked = []
    for result in candidates.values():
        if not matches_scope(result, scope):
            continue
        score = name_score(clean_query, result.name)
        if score and exact_name(clean_query, result):
            score = max(112, score)
        if score:
            score += recent_ranks.get(result.path, 0) + (8 if result.path in favorite_paths else 0)
            ranked.append((-score, len(result.name), result.name.casefold(), result.path, result))
    return [row[-1] for row in heapq.nsmallest(limit, ranked)]
