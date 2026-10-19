#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate Disk Warden IDE catalog and name-matching rules.

Checks performed:
  * JSON syntax and duplicate object keys
  * Required schema fields and enum values
  * Path placeholders (%APPDATA%, %USERPROFILE%, %LOCALAPPDATA%),
    no hard-coded drive letters or user names, no empty / dot segments
  * Directory type (dir / file / glob) consistency with the path
  * Duplicate IDE ids, duplicate paths and duplicate rule patterns
  * Rule patterns compile and required safety rules exist
  * Red name rules / credential, config, memory names are never marked
    green or yellow in the catalog
  * Coverage of the IDEs named in the acceptance criteria, including both
    the old Trae CN and new TRAE SOLO CN roots

Exit code is 0 when the data is valid, 1 when any error is found.

Usage:
    python tools/validate_rules.py
    python tools/validate_rules.py --catalog data/ide-catalog.json --rules data/rules.json
"""

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = ROOT / "data" / "ide-catalog.json"
DEFAULT_RULES = ROOT / "data" / "rules.json"

SAFETY_VALUES = ("green", "red", "yellow")
TYPE_VALUES = ("dir", "file", "glob")
HANDLING_VALUES = ("junction", "env-var", "app-config", "docker-settings")
KIND_VALUES = ("ide", "cli", "extension", "tool")
PLATFORM_VALUES = ("windows", "macos", "linux")
MATCH_TYPES = ("exact", "glob", "regex")
ALLOWED_PLACEHOLDERS = ("%APPDATA%", "%USERPROFILE%", "%LOCALAPPDATA%")

MIN_IDES = 20
MIN_DIRECTORIES = 100

REQUIRED_IDES = (
    "cursor",
    "trae-cn",
    "trae",
    "windsurf",
    "kiro",
    "vscode",
    "claude-code",
    "codex",
    "kimi",
    "kimi-code",
    "gemini-cli",
    "continue",
    "factory-ai",
    "devin",
    "deepseek-cli",
    "deepseek-harness",
    "qoder",
    "claude-desktop",
    "opencode",
)

REQUIRED_GREEN_PATTERNS = ("cache", "cacheddata", "gpucache", "logs", "sessions")
REQUIRED_RED_PATTERNS = ("login data", "settings.json")

# Names that must never be anything other than red, even if a rule is missing.
CRITICAL_NAMES = {
    "login data",
    "local state",
    "settings.json",
    "settings.local.json",
    ".credentials.json",
    ".credentials.yaml",
    ".claude.json",
    "auth.json",
    ".env",
    "config.toml",
    "kimi.json",
    "settings.yaml",
    "memory",
    "memories",
}

PLACEHOLDER_RE = re.compile(r"%[A-Za-z_][A-Za-z0-9_]*%")
SEGMENT_PLACEHOLDER_RE = re.compile(r"^(?:<[^<>\s]+>)+$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
INVALID_SEGMENT_CHARS = re.compile(r'[<>:"|?]')
CONTROL_CHARS = re.compile(r"[\x00-\x1f]")


class DuplicateKeyError(Exception):
    def __init__(self, key):
        Exception.__init__(self, key)
        self.key = key


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(key)
        result[key] = value
    return result


def display_path(path):
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def load_json(path, errors):
    label = display_path(path)
    if not path.exists():
        errors.append("%s: file not found" % label)
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=_no_duplicate_keys)
    except DuplicateKeyError as exc:
        errors.append("%s: duplicate JSON object key %r" % (label, exc.key))
    except json.JSONDecodeError as exc:
        errors.append(
            "%s: invalid JSON at line %d column %d: %s"
            % (label, exc.lineno, exc.colno, exc.msg)
        )
    except OSError as exc:
        errors.append("%s: cannot read file: %s" % (label, exc))
    return None


def is_non_empty_string(value, min_length=1):
    return isinstance(value, str) and len(value.strip()) >= min_length


def validate_path(path, entry_type, location, errors):
    if not is_non_empty_string(path):
        errors.append("%s: path must be a non-empty string" % location)
        return

    if path != path.strip():
        errors.append("%s: path has leading or trailing whitespace" % location)
    if "/" in path:
        errors.append("%s: path must use backslashes on Windows" % location)
    if re.match(r"^[A-Za-z]:", path):
        errors.append("%s: path must not contain a hard-coded drive letter" % location)
    if re.search(r"\\Users\\", path, re.IGNORECASE):
        errors.append("%s: path must not contain a hard-coded user name" % location)

    placeholders = PLACEHOLDER_RE.findall(path)
    if not placeholders:
        errors.append(
            "%s: path must start with one of %s"
            % (location, ", ".join(ALLOWED_PLACEHOLDERS))
        )
        return

    unknown = [item for item in placeholders if item not in ALLOWED_PLACEHOLDERS]
    if unknown:
        errors.append(
            "%s: unsupported placeholder(s): %s" % (location, ", ".join(unknown))
        )

    prefix = placeholders[0]
    if prefix not in ALLOWED_PLACEHOLDERS:
        return
    if not path.startswith(prefix + "\\"):
        errors.append("%s: path must start with %s\\" % (location, prefix))
        return

    rest = path[len(prefix):]
    if not rest.startswith("\\"):
        errors.append("%s: missing separator after %s" % (location, prefix))
        return
    if "\\\\" in rest:
        errors.append("%s: path contains an empty segment" % location)

    if entry_type == "dir":
        if not path.endswith("\\"):
            errors.append("%s: directory paths must end with a backslash" % location)
        body = rest.strip("\\")
    elif entry_type in ("file", "glob"):
        if path.endswith("\\"):
            errors.append("%s: %s paths must not end with a backslash" % (location, entry_type))
        body = rest.lstrip("\\")
    else:
        return

    if not body:
        errors.append("%s: path has no segments after the placeholder" % location)
        return
    if "%" in body:
        errors.append("%s: placeholders are only allowed at the start of the path" % location)

    segments = body.split("\\")
    for index, segment in enumerate(segments):
        seg_location = "%s segment %r" % (location, segment)
        if not segment:
            errors.append("%s: empty segment" % seg_location)
            continue
        if segment in (".", ".."):
            errors.append("%s: relative path segment is not allowed" % seg_location)
        if CONTROL_CHARS.search(segment):
            errors.append("%s: control character in path" % seg_location)

        has_wildcard = "*" in segment
        if has_wildcard:
            if entry_type != "glob":
                errors.append(
                    "%s: wildcard is only allowed for type glob" % seg_location
                )
            if segment in ("*", "**") or "**" in segment:
                errors.append("%s: wildcard is too broad" % seg_location)
            if index != len(segments) - 1:
                errors.append("%s: wildcard is only allowed in the last segment" % seg_location)

        has_placeholder = "<" in segment or ">" in segment
        if has_placeholder:
            if not SEGMENT_PLACEHOLDER_RE.match(segment):
                errors.append(
                    "%s: malformed segment placeholder, expected <name>"
                    % seg_location
                )
        else:
            invalid = INVALID_SEGMENT_CHARS.search(segment)
            if invalid:
                errors.append(
                    "%s: invalid character %r in path segment"
                    % (seg_location, invalid.group(0))
                )

    if entry_type == "glob" and "*" not in body:
        errors.append("%s: glob path contains no wildcard" % location)


def collect_catalog(data, errors):
    """Validate the catalog. Returns (ide_count, directory_count, safety_counts)."""
    safety_counts = {"green": 0, "red": 0, "yellow": 0}
    if not isinstance(data, dict):
        errors.append("catalog: top level must be a JSON object")
        return 0, 0, safety_counts

    if data.get("schema_version") != 1:
        errors.append("catalog: schema_version must be 1")
    if data.get("platform") not in PLATFORM_VALUES:
        errors.append("catalog: platform must be one of %s" % (PLATFORM_VALUES,))

    ides = data.get("ides")
    if not isinstance(ides, list):
        errors.append("catalog: ides must be an array")
        return 0, 0, safety_counts
    if len(ides) < MIN_IDES:
        errors.append(
            "catalog: expected at least %d IDE entries, found %d"
            % (MIN_IDES, len(ides))
        )

    seen_ids = {}
    seen_paths = {}
    total_directories = 0
    used_placeholders = set()
    roots_by_ide = {}

    for ide_index, ide in enumerate(ides):
        ide_location = "ides[%d]" % ide_index
        if not isinstance(ide, dict):
            errors.append("%s: must be an object" % ide_location)
            continue

        ide_id = ide.get("id")
        if not is_non_empty_string(ide_id):
            errors.append("%s.id: must be a non-empty string" % ide_location)
        else:
            if not ID_RE.match(ide_id):
                errors.append(
                    "%s.id: %r must match %s" % (ide_location, ide_id, ID_RE.pattern)
                )
            if ide_id in seen_ids:
                errors.append(
                    "%s.id: duplicate id %r (already used by ides[%d])"
                    % (ide_location, ide_id, seen_ids[ide_id])
                )
            else:
                seen_ids[ide_id] = ide_index
            ide_location = "ides[%d](%s)" % (ide_index, ide_id)

        if not is_non_empty_string(ide.get("name")):
            errors.append("%s.name: must be a non-empty string" % ide_location)
        if ide.get("kind") not in KIND_VALUES:
            errors.append(
                "%s.kind: must be one of %s" % (ide_location, ", ".join(KIND_VALUES))
            )
        if ide.get("platform") not in PLATFORM_VALUES:
            errors.append(
                "%s.platform: must be one of %s"
                % (ide_location, ", ".join(PLATFORM_VALUES))
            )

        processes = ide.get("processes")
        if not isinstance(processes, list):
            errors.append("%s.processes: must be an array" % ide_location)
        else:
            seen_processes = set()
            for process_index, process in enumerate(processes):
                process_location = "%s.processes[%d]" % (ide_location, process_index)
                if not is_non_empty_string(process):
                    errors.append("%s: must be a non-empty string" % process_location)
                    continue
                if "\\" in process or "/" in process:
                    errors.append("%s: must be a process name, not a path" % process_location)
                lowered = process.lower()
                if lowered in seen_processes:
                    errors.append("%s: duplicate process name %r" % (process_location, process))
                seen_processes.add(lowered)

        directories = ide.get("directories")
        if not isinstance(directories, list) or not directories:
            errors.append("%s.directories: must be a non-empty array" % ide_location)
            continue

        for dir_index, entry in enumerate(directories):
            entry_location = "%s.directories[%d]" % (ide_location, dir_index)
            if not isinstance(entry, dict):
                errors.append("%s: must be an object" % entry_location)
                continue

            path = entry.get("path")
            entry_type = entry.get("type")
            safety = entry.get("safety")
            reason = entry.get("reason")

            if entry_type not in TYPE_VALUES:
                errors.append(
                    "%s.type: must be one of %s"
                    % (entry_location, ", ".join(TYPE_VALUES))
                )
            if safety not in SAFETY_VALUES:
                errors.append(
                    "%s.safety: must be one of %s"
                    % (entry_location, ", ".join(SAFETY_VALUES))
                )
                safety = None
            if not is_non_empty_string(reason, min_length=3):
                errors.append("%s.reason: must be a non-empty string" % entry_location)
            if "handling" in entry and entry["handling"] not in HANDLING_VALUES:
                errors.append(
                    "%s.handling: must be one of %s"
                    % (entry_location, ", ".join(HANDLING_VALUES))
                )

            validate_path(path, entry_type, entry_location, errors)

            if is_non_empty_string(path):
                lowered_path = path.lower()
                if lowered_path in seen_paths:
                    errors.append(
                        "%s.path: duplicate path %r (already used by %s)"
                        % (entry_location, path, seen_paths[lowered_path])
                    )
                else:
                    seen_paths[lowered_path] = entry_location
                for token in PLACEHOLDER_RE.findall(path):
                    used_placeholders.add(token)
                if ide_id:
                    roots_by_ide.setdefault(ide_id, set()).add(path)

            if safety:
                safety_counts[safety] += 1
            total_directories += 1

    if total_directories < MIN_DIRECTORIES:
        errors.append(
            "catalog: expected at least %d directory entries, found %d"
            % (MIN_DIRECTORIES, total_directories)
        )

    for ide_id in REQUIRED_IDES:
        if ide_id not in seen_ids:
            errors.append("catalog: required IDE id %r is missing" % ide_id)

    for token in ALLOWED_PLACEHOLDERS:
        if token not in used_placeholders:
            errors.append("catalog: placeholder %s is never used" % token)

    trae_roots = roots_by_ide.get("trae-cn", set())
    if not any(path.lower().startswith("%appdata%\\trae cn\\") for path in trae_roots):
        errors.append("catalog: trae-cn must include the old %APPDATA%\\Trae CN\\ root")
    if not any(
        path.lower().startswith("%appdata%\\trae solo cn\\") for path in trae_roots
    ):
        errors.append(
            "catalog: trae-cn must include the new %APPDATA%\\TRAE SOLO CN\\ root"
        )

    return len(ides), total_directories, safety_counts


def compile_rule(rule):
    match_type = rule.get("match_type")
    pattern = rule.get("match")
    if match_type == "regex":
        return re.compile(pattern)
    return None


def validate_rules(data, errors):
    if not isinstance(data, dict):
        errors.append("rules: top level must be a JSON object")
        return 0

    if data.get("schema_version") != 1:
        errors.append("rules: schema_version must be 1")
    if data.get("name_scope") != "basename":
        errors.append("rules: name_scope must be basename")
    if data.get("case_sensitive") is not False:
        errors.append("rules: case_sensitive must be false on Windows")
    if data.get("conflict_resolution") != "red_wins":
        errors.append("rules: conflict_resolution must be red_wins")

    default = data.get("default")
    if not isinstance(default, dict):
        errors.append("rules: default must be an object")
    else:
        if default.get("safety") != "yellow":
            errors.append("rules: default.safety must be yellow for unknown names")
        if not is_non_empty_string(default.get("reason"), min_length=3):
            errors.append("rules: default.reason must be a non-empty string")

    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        errors.append("rules: rules must be a non-empty array")
        return 0

    seen_patterns = set()
    safety_counts = {"green": 0, "red": 0, "yellow": 0}
    green_matches = set()
    red_matches = set()

    for rule_index, rule in enumerate(rules):
        location = "rules[%d]" % rule_index
        if not isinstance(rule, dict):
            errors.append("%s: must be an object" % location)
            continue

        pattern = rule.get("match")
        match_type = rule.get("match_type")
        safety = rule.get("safety")
        reason = rule.get("reason")

        if not is_non_empty_string(pattern):
            errors.append("%s.match: must be a non-empty string" % location)
            pattern = None
        else:
            if pattern != pattern.strip():
                errors.append("%s.match: must not have leading or trailing whitespace" % location)
            if "\\" in pattern or "/" in pattern:
                errors.append("%s.match: must be a file or directory name, not a path" % location)
            if "%" in pattern:
                errors.append("%s.match: must not contain a placeholder" % location)
            if match_type == "exact" and "*" in pattern:
                errors.append("%s.match: exact rules must not contain a wildcard" % location)

        if match_type not in MATCH_TYPES:
            errors.append(
                "%s.match_type: must be one of %s"
                % (location, ", ".join(MATCH_TYPES))
            )
        if safety not in SAFETY_VALUES:
            errors.append(
                "%s.safety: must be one of %s"
                % (location, ", ".join(SAFETY_VALUES))
            )
        if not is_non_empty_string(reason, min_length=3):
            errors.append("%s.reason: must be a non-empty string" % location)

        if pattern is not None and match_type in MATCH_TYPES:
            key = (match_type, pattern.lower())
            if key in seen_patterns:
                errors.append(
                    "%s: duplicate rule for %s %r" % (location, match_type, pattern)
                )
            seen_patterns.add(key)

            if match_type == "regex":
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append("%s.match: invalid regex: %s" % (location, exc))
            if match_type == "glob" and "*" not in pattern:
                errors.append("%s.match: glob rule contains no wildcard" % location)

        if safety in safety_counts:
            safety_counts[safety] += 1
        if pattern is not None and safety == "green":
            green_matches.add(pattern.lower())
        if pattern is not None and safety == "red":
            red_matches.add(pattern.lower())

    for pattern in REQUIRED_GREEN_PATTERNS:
        if pattern not in green_matches:
            errors.append(
                "rules: required green rule %r is missing" % pattern
            )
    for pattern in REQUIRED_RED_PATTERNS:
        if pattern not in red_matches:
            errors.append("rules: required red rule %r is missing" % pattern)
    if not any("credential" in item for item in red_matches):
        errors.append("rules: no red rule covers credentials")

    return len(rules)


def rule_safety_for_name(name, rules):
    """Return (safety, matched_rule) with red taking precedence, or (None, None)."""
    red_match = None
    green_match = None
    lowered = name.lower()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("match")
        match_type = rule.get("match_type")
        safety = rule.get("safety")
        if not isinstance(pattern, str):
            continue
        lowered_pattern = pattern.lower()
        matched = False
        if match_type == "exact":
            matched = lowered == lowered_pattern
        elif match_type == "glob":
            matched = fnmatch.fnmatchcase(lowered, lowered_pattern)
        elif match_type == "regex":
            try:
                matched = re.fullmatch(pattern, name, re.IGNORECASE) is not None
            except re.error:
                matched = False
        if not matched:
            continue
        if safety == "red":
            red_match = rule
        elif safety == "green" and green_match is None:
            green_match = rule

    if red_match is not None:
        return "red", red_match
    if green_match is not None:
        return "green", green_match
    return None, None


def basename(path):
    return path.rstrip("\\").split("\\")[-1]


def cross_validate(catalog, rules_data, errors, warnings):
    if not isinstance(catalog, dict) or not isinstance(rules_data, dict):
        return
    ides = catalog.get("ides")
    rules = rules_data.get("rules")
    if not isinstance(ides, list) or not isinstance(rules, list):
        return

    red_count = 0
    yellow_count = 0
    for ide_index, ide in enumerate(ides):
        if not isinstance(ide, dict):
            continue
        ide_id = ide.get("id", "ides[%d]" % ide_index)
        directories = ide.get("directories")
        if not isinstance(directories, list):
            continue
        for dir_index, entry in enumerate(directories):
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            safety = entry.get("safety")
            if not isinstance(path, str) or not isinstance(safety, str):
                continue
            location = "%s.directories[%d]" % (ide_id, dir_index)
            name = basename(path)
            expected, matched_rule = rule_safety_for_name(name, rules)
            if expected is not None and safety != expected:
                errors.append(
                    "%s: %r is marked %s but name rule %r requires %s"
                    % (
                        location,
                        name,
                        safety,
                        matched_rule.get("match"),
                        expected,
                    )
                )
            if name.lower() in CRITICAL_NAMES and safety != "red":
                errors.append(
                    "%s: critical credential/config/memory name %r must be red, got %s"
                    % (location, name, safety)
                )
            if safety == "red":
                red_count += 1
            elif safety == "yellow":
                yellow_count += 1

    if yellow_count == 0:
        warnings.append(
            "catalog: no yellow entries found; unknown directories should fall back to yellow"
        )
    if red_count == 0:
        errors.append("catalog: no red entries found; credentials/config/memory must be red")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate Disk Warden IDE catalog and rule data."
    )
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG), help="path to ide-catalog.json")
    parser.add_argument("--rules", default=str(DEFAULT_RULES), help="path to rules.json")
    parser.add_argument("--quiet", action="store_true", help="only print errors and the final status")
    args = parser.parse_args(argv)

    catalog_path = Path(args.catalog)
    rules_path = Path(args.rules)

    errors = []
    warnings = []

    catalog = load_json(catalog_path, errors)
    rules_data = load_json(rules_path, errors)

    ide_count = 0
    directory_count = 0
    safety_counts = {"green": 0, "red": 0, "yellow": 0}
    rule_count = 0
    if catalog is not None:
        ide_count, directory_count, safety_counts = collect_catalog(catalog, errors)
    if rules_data is not None:
        rule_count = validate_rules(rules_data, errors)
        cross_validate(catalog, rules_data, errors, warnings)

    if not args.quiet:
        print("Catalog: %s" % display_path(catalog_path))
        print("Rules:   %s" % display_path(rules_path))

    if errors:
        print("")
        print("Validation failed with %d error(s):" % len(errors))
        for error in errors:
            print("  - %s" % error)
        if warnings:
            print("")
            print("%d warning(s):" % len(warnings))
            for warning in warnings:
                print("  - %s" % warning)
        return 1

    if warnings:
        print("")
        print("%d warning(s):" % len(warnings))
        for warning in warnings:
            print("  - %s" % warning)

    if not args.quiet:
        print("")
        print("IDEs:         %d" % ide_count)
        print("Directories:  %d" % directory_count)
        print(
            "Safety:       green=%d red=%d yellow=%d"
            % (
                safety_counts["green"],
                safety_counts["red"],
                safety_counts["yellow"],
            )
        )
        print("Rules:        %d" % rule_count)
    print("OK: validation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
