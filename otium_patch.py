#!/usr/bin/env python3
"""Create, diagnose, verify and apply review-required Otium source bundles."""

import argparse
import configparser
from collections import OrderedDict
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
from urllib.parse import urlsplit


# The supplied source archive includes the six-file January 2017 correction,
# not just the preceding import. Pin its complete verified snapshot.
DEFAULT_BASE = "27beaed70817d3edd86769007e1a14ea9cfdb806"
FORMAT = "otium-source-bundle-prototype-1"
DELTA_LIMIT = 4 * 1024 * 1024
CACHE_LIMIT = 32 * 1024 * 1024
TOOL_DIR = Path(__file__).resolve().parent
BUNDLE_ROOT = TOOL_DIR
README_TEMPLATE = TOOL_DIR / "PATCH_README.md"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def resolve_commit(repo, ref):
    return git(repo, "rev-parse", "--verify", "--end-of-options",
               ref + "^{commit}").decode("ascii").strip()


def safe_path(path):
    if (not isinstance(path, str) or not path or "\\" in path or ":" in path
            or "\x00" in path or PurePosixPath(path).is_absolute()
            or any(part in ("", ".", "..") or part.lower() == ".git"
                   for part in path.split("/"))):
        raise ValueError("Unsafe repository path: %r" % path)
    return path


def tree(repo, commit):
    entries = {}
    for record in git(repo, "ls-tree", "-r", "-z", commit).split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        path = safe_path(name.decode("utf-8"))
        if (mode, kind) not in (("100644", "blob"), ("100755", "blob"),
                                ("120000", "blob"), ("160000", "commit")):
            raise ValueError("Unsupported tree entry: " + path)
        entries[path] = {"mode": mode, "kind": kind, "oid": oid}
    return entries


class Blobs:
    """Read raw Git blobs without checkout filters, with a bounded cache."""

    def __init__(self, repo):
        self.process = subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        self.cache = OrderedDict()
        self.cached_bytes = 0

    def get(self, oid):
        if oid in self.cache:
            self.cache.move_to_end(oid)
            return self.cache[oid]
        self.process.stdin.write(oid.encode("ascii") + b"\n")
        self.process.stdin.flush()
        header = self.process.stdout.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise ValueError("Cannot read Git blob " + oid)
        size = int(header[2])
        data = self.process.stdout.read(size)
        if len(data) != size or self.process.stdout.read(1) != b"\n":
            raise ValueError("Truncated Git blob " + oid)
        if size <= CACHE_LIMIT:
            while self.cache and self.cached_bytes + size > CACHE_LIMIT:
                _, evicted = self.cache.popitem(last=False)
                self.cached_bytes -= len(evicted)
            self.cache[oid] = data
            self.cached_bytes += size
        return data

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.process.stdin.close()
        self.process.stdout.close()
        self.process.wait()


def checkout_candidate(path, record, ignore=False):
    """Bound work by the declared baseline; never write to the checkout."""
    candidate = bytearray()
    raw_hash = hashlib.sha256()
    observed = 0
    pending = b""
    text = True
    converted = False
    printable = 0
    nonprintable = 0
    last = b""
    # A CRLF checkout can be at most twice the size of its LF baseline.
    limit = path.stat().st_size if ignore else 2 * record["size"]
    candidate_limit = limit if ignore else record["size"]
    # Match Git's autocrlf binary heuristic (convert.c, gather_stats):
    # BS, TAB, FF and ESC count as printable; a final DOS EOF does not.
    controls = bytes((*range(8), 11, *range(14, 27), *range(28, 32), 127))
    with path.open("rb") as source:
        while True:
            block = source.read(min(65536, limit - observed + 1))
            if not block:
                break
            observed += len(block)
            if observed > limit:
                raise ValueError("Original source exceeds CRLF size bound: " + str(path))
            raw_hash.update(block)
            last = block[-1:]
            if b"\x00" in block:
                text = False
            block = pending + block
            pending = b"\r" if block.endswith(b"\r") else b""
            if pending:
                block = block[:-1]
            converted |= b"\r\n" in block
            block = block.replace(b"\r\n", b"\n")
            if b"\r" in block:
                text = False
            count = sum(block.count(bytes((c,))) for c in controls)
            nonprintable += count
            printable += len(block) - block.count(b"\n") - block.count(b"\r") - count
            if len(candidate) + len(block) <= candidate_limit:
                candidate.extend(block)
            else:
                text = False
    if pending:
        text = False
    if last == b"\x1a":
        nonprintable -= 1
    if printable // 128 < nonprintable:
        text = False
    verified = len(candidate) == record["size"] and digest(candidate) == record["sha256"]
    if text and converted and (verified or ignore):
        return bytes(candidate), observed, raw_hash.hexdigest()
    return None, observed, raw_hash.hexdigest()


def matching_edges(source, target):
    """Find common byte prefix/suffix in linear time, without overlapping."""
    limit = min(len(source), len(target))
    prefix = 0
    while prefix + 4096 <= limit and source[prefix:prefix + 4096] == target[prefix:prefix + 4096]:
        prefix += 4096
    while prefix < limit and source[prefix] == target[prefix]:
        prefix += 1
    suffix = 0
    remaining = limit - prefix
    while suffix + 4096 <= remaining and source[len(source) - suffix - 4096:len(source) - suffix] == target[len(target) - suffix - 4096:len(target) - suffix]:
        suffix += 4096
    while suffix < remaining and source[len(source) - suffix - 1] == target[len(target) - suffix - 1]:
        suffix += 1
    return prefix, suffix


def segments(source, target):
    """Yield COPY ranges or literal bytes; never use quadratic text matching.

    Exact lines can be reused even when moved within a file. Changed middle
    text remains literal and requires review. Large/binary inputs use only
    common prefix/suffix ranges, bounding indexing memory and CPU cost.
    """
    if source == target:
        if source:
            yield ("copy", 0, len(source))
        return
    prefix, suffix = matching_edges(source, target)
    if prefix:
        yield ("copy", 0, prefix)
    end = len(target) - suffix
    middle = target[prefix:end]
    if max(len(source), len(target)) <= DELTA_LIMIT and b"\0" not in source and b"\0" not in target:
        lines = {}
        offset = 0
        for line in source.splitlines(keepends=True):
            lines.setdefault(line, offset)
            offset += len(line)
        for line in middle.splitlines(keepends=True):
            if line in lines:
                yield ("copy", lines[line], len(line))
            else:
                yield ("add", line)
    elif middle:
        yield ("add", middle)
    if suffix:
        yield ("copy", len(source) - suffix, suffix)


def encode(source_path, source, target, payload_dir):
    operations = []
    literal = bytearray()
    for segment in segments(source, target):
        if segment[0] == "add":
            if operations and operations[-1]["op"] == "add":
                operations[-1]["size"] += len(segment[1])
            else:
                operations.append({"op": "add", "offset": len(literal),
                                   "size": len(segment[1])})
            literal.extend(segment[1])
        else:
            _, offset, length = segment
            if (operations and operations[-1]["op"] == "copy"
                    and operations[-1]["offset"] + operations[-1]["size"] == offset):
                operations[-1]["size"] += length
            else:
                operations.append({"op": "copy", "path": source_path,
                                   "offset": offset, "size": length})
    if literal:
        name = digest(literal)
        path = payload_dir / name
        if not path.exists():
            path.write_bytes(literal)
        for operation in operations:
            if operation["op"] == "add":
                operation["sha256"] = name
    return operations


def reconstruct(entry, sources, payload_dir, output=None, target_name=None):
    """Verify the reconstructed bytes and optionally send them to a binary stream."""
    result_hash = hashlib.sha256()
    size = 0
    payloads = {}
    unverified_source = False
    for operation in entry["operations"]:
        length = operation.get("size")
        if type(length) is not int or length <= 0 or size + length > entry["size"]:
            raise ValueError("Invalid operation length")
        if operation["op"] == "copy":
            source_name = safe_path(operation["path"])
            data = sources(source_name)
            unverified_source |= source_name in getattr(sources, "ignored", ())
            offset = operation["offset"]
            if type(offset) is not int or offset < 0 or offset + length > len(data):
                raise ValueError("COPY outside source")
            data = memoryview(data)[offset:offset + length]
        elif operation["op"] == "add":
            name = operation["sha256"]
            if not isinstance(name, str) or len(name) != 64 or any(c not in "0123456789abcdef" for c in name):
                raise ValueError("Invalid payload hash")
            if name not in payloads:
                path = payload_dir / name
                if path.is_symlink() or path.resolve().parent != payload_dir.resolve():
                    raise ValueError("Payload escapes bundle")
                data = path.read_bytes()
                if digest(data) != name:
                    raise ValueError("Corrupt payload " + name)
                payloads[name] = data
            data = payloads[name]
            # The first prototype export used a whole payload per ADD.
            offset = operation.get("offset", 0)
            if type(offset) is not int or offset < 0 or offset + length > len(data):
                raise ValueError("ADD outside payload")
            data = memoryview(data)[offset:offset + length]
        else:
            raise ValueError("Unknown operation")
        result_hash.update(data)
        if output is not None:
            output.write(data)
        size += length
    if size != entry["size"]:
        raise ValueError("Reconstructed content does not match target")
    if result_hash.hexdigest() != entry["sha256"]:
        if not unverified_source:
            raise ValueError("Reconstructed content does not match target")
        print("Warning: --ignore: unverified reconstructed target %s (expected SHA-256 %s, observed %s)" % (
            target_name or "<unnamed>", entry["sha256"], result_hash.hexdigest()), file=sys.stderr, flush=True)


def export(repo, base_ref, output_parent, progress=True):
    repo = Path(git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    base = resolve_commit(repo, base_ref)
    target = resolve_commit(repo, "HEAD")
    git(repo, "merge-base", "--is-ancestor", base, target)
    baseline = tree(repo, base)
    target_tree = tree(repo, target)
    support = {
        "README.md": README_TEMPLATE,
        "otium_patch.py": Path(__file__).resolve(),
        "patch.bat": TOOL_DIR / "patch.bat",
        "patch.sh": TOOL_DIR / "patch.sh",
    }
    for path in support.values():
        if not path.is_file():
            raise ValueError("Missing bundle support file: " + str(path))
    by_oid = {}
    for path, item in baseline.items():
        if item["kind"] == "blob":
            by_oid.setdefault(item["oid"], path)
    parent = Path(output_parent).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    bundle = parent / ("otium-patch-" + target)
    bundle.mkdir()  # Never overwrite an existing bundle, even an incomplete one.
    try:
        payload_dir = bundle / "payloads"
        payload_dir.mkdir()
        manifest = {
            "format": FORMAT,
            "publication_status": "prototype-requires-content-and-rights-review",
            "base_commit": base,
            "target_commit": target,
            "base_tree": git(repo, "rev-parse", base + "^{tree}").decode().strip(),
            "target_tree": git(repo, "rev-parse", target + "^{tree}").decode().strip(),
            "sources": {}, "files": {}, "submodules": {},
            "removed_paths": sorted(set(baseline) - set(target_tree)),
        }
        total = len(target_tree)
        copied_bytes = 0
        added_bytes = 0
        with Blobs(repo) as blobs:
            for index, (path, item) in enumerate(sorted(target_tree.items()), 1):
                if item["kind"] == "commit":
                    manifest["submodules"][path] = item["oid"]
                    continue
                target_data = blobs.get(item["oid"])
                source_path = by_oid.get(item["oid"])
                if source_path is None and path in baseline and baseline[path]["kind"] == "blob":
                    source_path = path
                source = blobs.get(baseline[source_path]["oid"]) if source_path else b""
                operations = encode(source_path, source, target_data, payload_dir)
                entry = {"mode": item["mode"], "size": len(target_data),
                         "sha256": digest(target_data), "operations": operations}
                for operation in operations:
                    if operation["op"] == "copy":
                        copied_bytes += operation["size"]
                        if source_path not in manifest["sources"]:
                            manifest["sources"][source_path] = {
                                "size": len(source), "sha256": digest(source),
                            }
                    else:
                        added_bytes += operation["size"]
                reconstruct(entry, lambda name: blobs.get(baseline[name]["oid"]), payload_dir)
                manifest["files"][path] = entry
                if progress and (index % 1000 == 0 or index == total):
                    print("Verified %d/%d entries" % (index, total), flush=True)
        payload_paths = list(payload_dir.iterdir())
        manifest["statistics"] = {
            "files": len(manifest["files"]), "submodules": len(manifest["submodules"]),
            "copy_bytes": copied_bytes, "add_bytes": added_bytes,
            "payload_files": len(payload_paths),
            "payload_bytes": sum(path.stat().st_size for path in payload_paths),
        }
        for name, original in support.items():
            destination = bundle / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, destination)
        (bundle / "patch.sh").chmod(0o755)
        # The manifest is written last; no incomplete export can be mistaken for a bundle.
        (bundle / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n",
        )
        return bundle, manifest["statistics"]
    except BaseException:
        # Only this invocation's exclusively created directory is removed.
        if bundle.parent == parent and not bundle.is_symlink():
            shutil.rmtree(bundle)
        raise


def verify(repo, bundle, progress=True):
    bundle = Path(bundle).resolve()
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if manifest["format"] != FORMAT:
        raise ValueError("Unsupported bundle format")
    baseline = tree(repo, resolve_commit(repo, manifest["base_commit"]))
    target = resolve_commit(repo, manifest["target_commit"])
    target_tree = tree(repo, target)
    if bundle.name != "otium-patch-" + target:
        raise ValueError("Bundle directory does not match target commit")
    for key, commit in (("base_tree", manifest["base_commit"]), ("target_tree", target)):
        if git(repo, "rev-parse", commit + "^{tree}").decode().strip() != manifest[key]:
            raise ValueError("Tree identity mismatch")
    expected_files = {p for p, e in target_tree.items() if e["kind"] == "blob"}
    expected_submodules = {p: e["oid"] for p, e in target_tree.items() if e["kind"] == "commit"}
    if set(manifest["files"]) != expected_files or manifest["submodules"] != expected_submodules:
        raise ValueError("Target inventory mismatch")
    if manifest["removed_paths"] != sorted(set(baseline) - set(target_tree)):
        raise ValueError("Removed-path inventory mismatch")
    payload_dir = bundle / "payloads"
    if payload_dir.is_symlink():
        raise ValueError("Payload directory must not be a symlink")
    referenced_payloads = set()
    referenced_sources = set()
    with Blobs(repo) as blobs:
        for path, record in manifest["sources"].items():
            safe_path(path)
            if path not in baseline or baseline[path]["kind"] != "blob":
                raise ValueError("Unknown baseline source: " + path)
            data = blobs.get(baseline[path]["oid"])
            if record != {"size": len(data), "sha256": digest(data)}:
                raise ValueError("Baseline source hash mismatch: " + path)

        def source(name):
            if name not in manifest["sources"]:
                raise ValueError("COPY references an undeclared source")
            referenced_sources.add(name)
            return blobs.get(baseline[name]["oid"])

        for index, (path, record) in enumerate(sorted(manifest["files"].items()), 1):
            data = blobs.get(target_tree[path]["oid"])
            if (record["mode"] != target_tree[path]["mode"] or record["size"] != len(data)
                    or record["sha256"] != digest(data)):
                raise ValueError("Target metadata mismatch: " + path)
            reconstruct(record, source, payload_dir)
            referenced_payloads.update(op["sha256"] for op in record["operations"] if op["op"] == "add")
            if progress and (index % 1000 == 0 or index == len(expected_files)):
                print("Verified %d/%d files" % (index, len(expected_files)), flush=True)
    if referenced_payloads != {path.name for path in payload_dir.iterdir()}:
        raise ValueError("Payload inventory mismatch")
    if referenced_sources != set(manifest["sources"]):
        raise ValueError("Unused source records")
    return len(expected_files)


def require_hash(value, lengths=(64,)):
    if (not isinstance(value, str) or len(value) not in lengths
            or any(c not in "0123456789abcdef" for c in value)):
        raise ValueError("Invalid hash")


def portable_path(path):
    safe_path(path)
    if os.name == "nt":
        reserved = {"CON", "PRN", "AUX", "NUL"}
        reserved.update("COM%d" % n for n in range(1, 10))
        reserved.update("LPT%d" % n for n in range(1, 10))
        for part in path.split("/"):
            if (part.endswith((".", " ")) or any(c in '<>"|?*' or ord(c) < 32 for c in part)
                    or part.split(".")[0].upper() in reserved):
                raise ValueError("Path is not representable on Windows: " + path)
    return path


def validate_record(record):
    if not isinstance(record, dict) or type(record.get("size")) is not int or record["size"] < 0:
        raise ValueError("Invalid file size")
    require_hash(record.get("sha256"))


def load_bundle(bundle):
    """Validate the declarative manifest without Git or source-tree access."""
    bundle = Path(bundle).resolve()
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise ValueError("Unsupported bundle format")
    for key in ("base_commit", "target_commit", "base_tree", "target_tree"):
        require_hash(manifest.get(key), (40, 64))
    if bundle.name != "otium-patch-" + manifest["target_commit"]:
        raise ValueError("Bundle directory does not match target commit")
    for key in ("files", "sources", "submodules"):
        if not isinstance(manifest.get(key), dict):
            raise ValueError("Invalid manifest inventory: " + key)
    target_paths = {}
    prefixes = set()
    for path in list(manifest["files"]) + list(manifest["submodules"]):
        portable_path(path)
        key = path.casefold() if os.name == "nt" else path
        if key in target_paths:
            raise ValueError("Conflicting target paths: " + path)
        target_paths[key] = path
        parts = key.split("/")
        prefixes.update("/".join(parts[:i]) for i in range(1, len(parts)))
    if prefixes.intersection(target_paths):
        raise ValueError("Target file/directory conflict")
    for path, record in manifest["sources"].items():
        portable_path(path)
        validate_record(record)
    for path in manifest.get("removed_paths", []):
        portable_path(path)
    for pin in manifest["submodules"].values():
        require_hash(pin, (40, 64))
    payload_names = set()
    source_names = set()
    for record in manifest["files"].values():
        validate_record(record)
        if record.get("mode") not in ("100644", "100755", "120000"):
            raise ValueError("Unsupported target file mode")
        if not isinstance(record.get("operations"), list):
            raise ValueError("Invalid operation list")
        size = 0
        for operation in record["operations"]:
            length = operation.get("size")
            offset = operation.get("offset", 0)
            if type(length) is not int or length <= 0 or type(offset) is not int or offset < 0:
                raise ValueError("Invalid operation range")
            size += length
            if operation.get("op") == "copy":
                name = safe_path(operation["path"])
                if name not in manifest["sources"] or offset + length > manifest["sources"][name]["size"]:
                    raise ValueError("COPY outside declared source")
                source_names.add(name)
            elif operation.get("op") == "add":
                require_hash(operation.get("sha256"))
                payload_names.add(operation["sha256"])
            else:
                raise ValueError("Unknown operation")
        if size != record["size"]:
            raise ValueError("Operation lengths do not match target size")
    if source_names != set(manifest["sources"]):
        raise ValueError("Unused source records")
    payload_dir = bundle / "payloads"
    if is_link(payload_dir) or not payload_dir.is_dir():
        raise ValueError("Payload directory must be an ordinary directory")
    if payload_names != {path.name for path in payload_dir.iterdir()}:
        raise ValueError("Payload inventory mismatch")
    for name in payload_names:
        path = payload_dir / name
        if is_link(path) or not path.is_file():
            raise ValueError("Payload must be an ordinary file: " + name)
    return manifest


def is_link(path):
    """Include Windows junctions and other reparse points, not only symlinks."""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


class DiskSources:
    """Read and hash-check local original files without following directory links."""

    def __init__(self, root, records, ignore=False):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Original source path is not a directory")
        self.records = records
        self.cache = OrderedDict()
        self.cached_bytes = 0
        self.normalized = set()
        self.ignore = ignore
        self.ignored = set()

    def __call__(self, name):
        if name in self.cache:
            self.cache.move_to_end(name)
            return self.cache[name]
        record = self.records[name]
        current = self.root
        parts = safe_path(name).split("/")
        for part in parts[:-1]:
            current = current / part
            if is_link(current) or not current.is_dir():
                raise ValueError("Source directory must not be a link: " + name)
        path = current / parts[-1]
        info = path.lstat()
        regular = False
        if path.is_symlink():
            data = os.fsencode(os.readlink(path))
        elif is_link(path) or not stat.S_ISREG(info.st_mode):
            raise ValueError("Unsupported original source entry: " + name)
        else:
            regular = True
            data = b""
            if info.st_size == record["size"]:
                with path.open("rb") as source:
                    data = source.read(record["size"] + 1)
        if (regular and info.st_size != record["size"]) or len(data) != record["size"] or digest(data) != record["sha256"]:
            observed_size = info.st_size if regular and info.st_size != record["size"] else len(data)
            observed_hash = digest(data) if not regular or info.st_size == len(data) else None
            data = None  # Do not retain a full raw file beside its candidate.
            if regular and record["size"] <= observed_size <= 2 * record["size"]:
                data, observed_size, observed_hash = checkout_candidate(path, record)
            if data is None:
                kind = "size" if observed_size != record["size"] else "hash"
                detail = "expected size %d, observed size %d; expected SHA-256 %s" % (
                    record["size"], observed_size, record["sha256"])
                if observed_hash is not None:
                    detail += ", observed SHA-256 " + observed_hash
                message = "Original source %s mismatch: %s (%s); no verified CRLF-to-LF match" % (
                    kind, name, detail)
                if not self.ignore:
                    raise ValueError(message)
                if regular:
                    # Unsafe mode still converts only Git-style text, without
                    # claiming that the candidate is the original baseline.
                    data, _, _ = checkout_candidate(path, record, ignore=True)
                    if data is None:
                        with path.open("rb") as source:
                            data = source.read(info.st_size + 1)
                        if len(data) != info.st_size:
                            raise ValueError("Source size changed while reading: " + name)
                else:
                    data = os.fsencode(os.readlink(path))
                if name not in self.ignored:
                    print("Warning: --ignore: " + message, file=sys.stderr, flush=True)
                    self.ignored.add(name)
            elif name not in self.normalized:
                print("Verified CRLF-to-LF checkout conversion: " + name, flush=True)
                self.normalized.add(name)
        if len(data) <= CACHE_LIMIT:
            while self.cache and self.cached_bytes + len(data) > CACHE_LIMIT:
                _, evicted = self.cache.popitem(last=False)
                self.cached_bytes -= len(evicted)
            self.cache[name] = data
            self.cached_bytes += len(data)
        return data


def symlink_target(path, data, link_paths):
    text = os.fsdecode(data)
    if not text or "\\" in text or ":" in text or "\x00" in text or text.startswith("/"):
        raise ValueError("Unsafe symbolic-link target: " + path)
    parts = path.split("/")[:-1]
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ValueError("Symbolic link escapes destination: " + path)
            parts.pop()
        else:
            portable_path(part)
            parts.append(part)
            # POSIX filesystems can also be case-insensitive. Reject aliases
            # conservatively rather than allowing traversal through a link.
            key = "/".join(parts).casefold()
            if key in link_paths:
                raise ValueError("Symbolic-link chains are unsupported: " + path)
    return text


def check_sources(bundle, manifest, sources, progress=True):
    """Preflight every output, including link targets, before creating a destination."""
    for name in manifest["sources"]:
        sources(name)
    links = {}
    link_paths = {p.casefold()
                  for p, r in manifest["files"].items() if r["mode"] == "120000"}
    for index, (name, record) in enumerate(sorted(manifest["files"].items()), 1):
        if record["mode"] == "120000":
            if record["size"] > 32768:
                raise ValueError("Oversized symbolic-link target: " + name)
            output = io.BytesIO()
            reconstruct(record, sources, bundle / "payloads", output, target_name=name)
            links[name] = symlink_target(name, output.getvalue(), link_paths)
        else:
            reconstruct(record, sources, bundle / "payloads", target_name=name)
        if progress and (index % 1000 == 0 or index == len(manifest["files"])):
            print("Checked %d/%d files" % (index, len(manifest["files"])), flush=True)
    return links


def overlaps(first, second):
    return first == second or first in second.parents or second in first.parents


def apply_bundle(bundle, source_root, destination, progress=True, ignore=False):
    bundle = Path(bundle).resolve(strict=True)
    manifest = load_bundle(bundle)
    sources = DiskSources(source_root, manifest["sources"], ignore=ignore)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination already exists; choose a new directory")
    # Require an existing parent, so cleanup never has to guess which parents it owns.
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    if overlaps(destination, sources.root) or overlaps(destination, bundle) or overlaps(sources.root, bundle):
        raise ValueError("Original sources, bundle and destination must be separate trees")
    links = check_sources(bundle, manifest, sources, progress)
    destination.mkdir()  # Exclusive: never overwrite even an empty existing directory.
    try:
        for index, (name, record) in enumerate(sorted(manifest["files"].items()), 1):
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if name not in links:
                with path.open("xb") as output:
                    reconstruct(record, sources, bundle / "payloads", output, target_name=name)
                if os.name != "nt":
                    path.chmod(int(record["mode"][-3:], 8))
            if progress and (index % 1000 == 0 or index == len(manifest["files"])):
                print("Wrote %d/%d entries" % (index, len(manifest["files"])), flush=True)
        # All regular writes have finished before any output symlink exists.
        for name, target in sorted(links.items()):
            path = destination / name
            try:
                os.symlink(target, path, target_is_directory=(path.parent / target).is_dir())
            except OSError as error:
                raise ValueError("Cannot create symbolic link %s; on Windows enable Developer Mode: %s" % (name, error)) from error
            if os.fsencode(os.readlink(path)) != os.fsencode(target):
                raise ValueError("Symbolic-link bytes changed during creation: " + name)
        return destination, manifest
    except BaseException:
        if destination.parent == parent and not is_link(destination):
            shutil.rmtree(destination)
        raise


def print_dependencies(manifest):
    if manifest["submodules"]:
        print("Submodule contents are not included. Supply these separately before building:")
        for path, pin in sorted(manifest["submodules"].items()):
            print("  %s @ %s" % (path, pin))


def dependency_plan(root, manifest):
    """Read the reconstructed Git configuration without requiring Git itself."""
    metadata = Path(root) / ".gitmodules"
    if is_link(metadata):
        raise ValueError("Dependency metadata must not be a link")
    parser = configparser.RawConfigParser()
    with metadata.open(encoding="utf-8") as stream:
        parser.read_file(stream)
    urls = {}
    for section in parser.sections():
        if not section.startswith('submodule "') or not section.endswith('"'):
            raise ValueError("Unsupported .gitmodules section: " + section)
        path = parser.get(section, "path")
        url = parser.get(section, "url")
        if path.startswith('"'):
            path = json.loads(path)
        if url.startswith('"'):
            url = json.loads(url)
        portable_path(path)
        # Do not execute Git's external transport helpers or infer relative
        # URLs from a nonexistent parent repository. Local file URLs also
        # support independently supplied dependencies and offline testing.
        address = urlsplit(url)
        if (any(ord(c) < 32 for c in url) or address.scheme not in ("https", "file")
                or (address.scheme == "https" and (not address.hostname or address.username is not None))):
            raise ValueError("Dependency URL must use https:// or file://: " + path)
        if path in urls:
            raise ValueError("Duplicate dependency path: " + path)
        urls[path] = url
    plans = []
    for path, pin in sorted(manifest["submodules"].items()):
        portable_path(path)
        require_hash(pin, (40, 64))
        if path not in urls:
            raise ValueError("No URL in .gitmodules for dependency: " + path)
        plans.append((path, urls[path], pin))
    return plans


def dependency_commands(root, plan):
    path, url, pin = plan
    return (["git", "-C", str(root), "clone", "--no-checkout", "--", url, path],
            ["git", "-C", str(Path(root) / path), "checkout", "--detach", pin])


def display_command(command):
    # Single quotes work in both PowerShell and POSIX shells, but their
    # embedded-quote escaping differs. Never turn printed text into a shell.
    escape = (lambda value: "'" + value.replace("'", "''") + "'") if os.name == "nt" else (
        lambda value: "'" + value.replace("'", "'\"'\"'") + "'")
    return "git " + " ".join(escape(value) for value in command[1:])


def restore_dependency(root, plan):
    """Own only a newly created clone directory; never overwrite an input."""
    root = Path(root).resolve(strict=True)
    path, url, pin = plan
    portable_path(path)
    require_hash(pin, (40, 64))
    parent = root
    created_parents = []
    destination = root / path
    owned = False

    def remove_readonly(function, name, error):
        # Git pack files are read-only on Windows. Retry only entries within
        # this invocation's newly created clone, never links or shared files.
        item = Path(name)
        if (os.name != "nt" or not isinstance(error[1], PermissionError)
                or function not in (os.unlink, os.rmdir)
                or not item.is_relative_to(destination) or is_link(item)):
            raise error[1]
        item.chmod(stat.S_IREAD | stat.S_IWRITE)
        function(name)

    try:
        for part in path.split("/")[:-1]:
            parent = parent / part
            if parent.exists() or parent.is_symlink():
                if is_link(parent) or not parent.is_dir():
                    raise ValueError("Dependency parent must be an ordinary directory: " + path)
            else:
                parent.mkdir()
                created_parents.append(parent)
        destination.mkdir()  # Exclusive, even an existing empty directory fails.
        owned = True
        print("Restoring dependency %s at %s" % (path, pin), flush=True)
        git(root, "clone", "--no-checkout", "--", url, path)
        git(destination, "checkout", "--detach", pin)
        if resolve_commit(destination, "HEAD") != pin:
            raise ValueError("Dependency checkout does not match recorded commit: " + path)
    except BaseException:
        if owned and destination.parent == parent and not is_link(destination):
            shutil.rmtree(destination, onerror=remove_readonly)
        for directory in reversed(created_parents):
            directory.rmdir()
        raise


def offer_dependencies(root, manifest):
    """Suggest pinned clones, with network writes only after terminal consent."""
    if not manifest["submodules"]:
        return
    print_dependencies(manifest)
    try:
        plans = dependency_plan(root, manifest)
    except (ValueError, OSError, configparser.Error) as error:
        print("Dependency commands unavailable: %s. The patched source tree is preserved; supply the listed commits manually." % error,
              file=sys.stderr)
        return
    print("The patched tree is not a Git repository; git submodule update cannot work here.")
    print("Run these commands in PowerShell on Windows or a POSIX shell on Linux:")
    for plan in plans:
        for command in dependency_commands(root, plan):
            print("  " + display_command(command))
    if not sys.stdin.isatty():
        print("Non-interactive input: commands shown only; no dependencies downloaded.")
        return
    if shutil.which("git") is None:
        print("Git is not installed or not on PATH; install it and run the commands above.")
        return
    try:
        confirmed = input("Download and check out these exact dependency commits now? [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        confirmed = False
    if not confirmed:
        print("Dependency download skipped; run the commands above before building.")
        return
    for plan in plans:
        try:
            restore_dependency(root, plan)
        except (ValueError, OSError) as error:
            raise ValueError("Dependency setup failed; patched source tree preserved at %s: %s" % (root, error)) from error
    print("Dependencies restored at their recorded commits. Follow README.md to build.")


def doctor(repo, base, bundle=None, source=None, ignore=False):
    print("Python: %s (%s)" % (sys.version.split()[0], sys.executable))
    if bundle is not None:
        bundle = Path(bundle).resolve()
        manifest = load_bundle(bundle)
        print("Bundle: " + str(bundle))
        print("Baseline: " + manifest["base_commit"])
        print("Target: " + manifest["target_commit"])
        print("Tracked files: %d" % len(manifest["files"]))
        print("Publication status: " + manifest.get("publication_status", "unspecified"))
        print_dependencies(manifest)
        if source is not None:
            sources = DiskSources(source, manifest["sources"], ignore=ignore)
            check_sources(bundle, manifest, sources)
            if sources.ignored:
                print("Unsafe --ignore preflight completed with %d mismatched sources; output is not guaranteed; no files written." % len(sources.ignored))
            else:
                print("Source and reconstruction checks passed; no files written.")
        else:
            print("Manifest checked. Add --source to verify original files and all reconstructed bytes.")
    else:
        if source is not None:
            raise ValueError("Repository launcher: --source requires --bundle <directory>; alternatively use patch.bat or patch.sh from the intact generated bundle")
        print(git(repo, "--version").decode().strip())
        print("Repository: " + git(repo, "rev-parse", "--show-toplevel").decode().strip())
        print("Baseline: " + resolve_commit(repo, base))
        print("Target: " + resolve_commit(repo, "HEAD"))
        print("Default output parent: " + str(Path.cwd()))
        for path in (README_TEMPLATE, TOOL_DIR / "patch.bat", TOOL_DIR / "patch.sh"):
            if not path.is_file():
                raise ValueError("Missing support file: " + str(path))
        print("Creation prerequisites found. No files written.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--output-parent", type=Path,
                        help="Default: current working directory")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--verify", type=Path,
                       help="Author check: verify an existing bundle against its raw Git trees")
    modes.add_argument("--apply", action="store_true", help="Reconstruct a new tree from original files")
    modes.add_argument("--doctor", action="store_true", help="Diagnose prerequisites or a bundle without writes")
    parser.add_argument("--bundle", type=Path, help="Bundle directory (auto-detected from the bundled launcher's location)")
    parser.add_argument("--source", type=Path, help="Unmodified original source directory")
    parser.add_argument("--destination", type=Path, help="New, non-existing output source directory")
    parser.add_argument("--ignore", action="store_true",
                        help="UNSAFE: allow mismatched sources and affected target hashes; output may be corrupt (--apply or --doctor --source only)")
    args = parser.parse_args()
    try:
        if sys.version_info < (3, 10):
            raise ValueError("Python 3.10 or newer is required")
        bundle = args.bundle
        if bundle is None and (BUNDLE_ROOT / "manifest.json").is_file():
            bundle = BUNDLE_ROOT
        if bundle is not None and not (bundle / "manifest.json").is_file():
            raise ValueError("Invalid bundle: %s; expected manifest.json in an intact generated bundle" % bundle)
        if args.source and not (args.apply or args.doctor):
            raise ValueError("--source requires --apply or --doctor")
        if args.destination and not args.apply:
            raise ValueError("--destination requires --apply")
        if args.ignore and not (args.apply or (args.doctor and args.source is not None)):
            raise ValueError("--ignore requires --apply or --doctor --source; creation and Git verification stay strict")
        if args.ignore:
            print("Warning: --ignore is unsafe: source mismatches and affected target hashes may be ignored; output may be corrupt", file=sys.stderr, flush=True)
        if args.doctor:
            doctor(args.repo, args.base, bundle, args.source, ignore=args.ignore)
            return 0
        if args.apply:
            missing = [name for name, value in (("--bundle", bundle), ("--source", args.source),
                                               ("--destination", args.destination)) if value is None]
            if missing:
                detail = ""
                if bundle is None:
                    detail = "; repository launchers require --bundle <directory>, or use the intact bundled launcher"
                else:
                    detail = "; selected bundle: " + str(bundle.resolve())
                raise ValueError("Application missing " + ", ".join(missing) + detail)
            destination, manifest = apply_bundle(bundle, args.source, args.destination, ignore=args.ignore)
            print("Applied %d files to %s" % (len(manifest["files"]), destination))
            offer_dependencies(destination, manifest)
            return 0
        if args.verify:
            count = verify(args.repo, args.verify)
            print("Verified %d tracked files; submodule pins checked separately." % count)
        else:
            if bundle is not None:
                raise ValueError("Use --apply or --doctor with a bundle; see README.md")
            root = Path(git(args.repo, "rev-parse", "--show-toplevel").decode().strip())
            parent = args.output_parent if args.output_parent else Path.cwd()
            bundle, stats = export(root, args.base, parent)
            print(str(bundle))
            print(json.dumps(stats, sort_keys=True))
    except MemoryError:
        print("Error: Insufficient memory; inputs and existing destinations were not modified", file=sys.stderr)
        return 1
    except (ValueError, OSError, KeyError, TypeError) as error:
        print("Error: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
