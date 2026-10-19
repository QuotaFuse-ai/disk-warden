# -*- coding: utf-8 -*-
"""Read-only CLI for the T06 discovery and T08 measurement layers.

Usage (from the repository root):
    python -m src.core.scan
    python -m src.core.scan --json
    python -m src.core.scan --tool cursor --verbose
    python -m src.core.scan --sizes --processes
    python -m src.core.scan --catalog data/ide-catalog.json
    python -m src.core.scan --ai --json

The command only stats/lists directories and enumerates processes.  It never
writes to the scanned locations and never touches the user's data.

``--ai`` is the explicit opt-in for the third-layer AI analysis (T22).  It is
off by default and stays off when no API key is configured; in that case the
command prints a single hint and continues with the original behaviour.

Python 3.8 compatible (decision 0005).
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from ..ai import (
    AIResolver,
    OpenAICompatibleProvider,
    load_config,
    stable_file_version,
)
from ..ai.cache import AICache
from ..rules import RulesError, load_engine
from ..rules.models import SAFETY_GREEN, SAFETY_RED, SAFETY_YELLOW, SOURCE_AI
from .catalog import CatalogError, load_catalog
from .discovery import discover
from .process import detect_processes, summarize as summarize_processes
from .size import measure, targets_from_discovery

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CATALOG = REPO_ROOT / "data" / "ide-catalog.json"
DEFAULT_RULES = REPO_ROOT / "data" / "rules.json"

AI_HINT_MISSING_KEY = (
    "AI 分析未启用：未配置 %s（或缺少 endpoint/model），已按原逻辑继续。"
    % "DISK_WARDEN_AI_API_KEY"
)
AI_HINT_NEEDS_SIZES = "AI 分析未启用：--ai 需要同时指定 --sizes。"


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m src.core.scan",
        description="List known IDEs/CLIs found via data/ide-catalog.json (read-only).",
    )
    parser.add_argument(
        "--catalog",
        default=str(DEFAULT_CATALOG),
        help="path to ide-catalog.json (default: data/ide-catalog.json)",
    )
    parser.add_argument(
        "--tool",
        action="append",
        default=None,
        help="only report this tool id; repeatable",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the full machine-readable result",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="list every catalog directory and its status",
    )
    parser.add_argument(
        "--sizes",
        action="store_true",
        help="recursively measure bytes and file counts of present entries",
    )
    parser.add_argument(
        "--processes",
        action="store_true",
        help="report running/unknown processes for each catalog tool",
    )
    parser.add_argument(
        "--ai",
        action="store_true",
        help="enable third-layer AI suggestions for yellow items (off by default)",
    )
    parser.add_argument(
        "--rules",
        default=str(DEFAULT_RULES),
        help="path to rules.json used by --ai (default: data/rules.json)",
    )
    parser.add_argument(
        "--ai-cache",
        default=None,
        help="override the AI cache file path (default: per-user cache dir)",
    )
    return parser


def _ai_logger():
    # type: () -> logging.Logger
    """AI diagnostics on stderr; messages never contain the key or absolute paths."""
    logger = logging.getLogger("disk_warden.ai")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("ai: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _build_ai_resolver(args, environ):
    # type: (argparse.Namespace, object) -> tuple
    """Build the resolver, or print one hint and return ``(None, config)``."""
    config = load_config(environ=environ, enabled=bool(args.ai))
    if args.ai_cache:
        config = config.__class__(
            enabled=config.enabled,
            base_url=config.base_url,
            model=config.model,
            api_key=config.api_key,
            max_calls=config.max_calls,
            max_total_tokens=config.max_total_tokens,
            max_output_tokens=config.max_output_tokens,
            timeout_seconds=config.timeout_seconds,
            ttl_seconds=config.ttl_seconds,
            max_path_segments=config.max_path_segments,
            cache_path=args.ai_cache,
        )
    if not config.enabled:
        return None, config
    if not config.available:
        print(AI_HINT_MISSING_KEY, file=sys.stderr)
        return None, config

    logger = _ai_logger()
    provider = OpenAICompatibleProvider(
        base_url=config.base_url,
        model=config.model,
        api_key=config.api_key,
        timeout_seconds=config.timeout_seconds,
    )
    cache = (
        AICache(config.cache_path, ttl_seconds=config.ttl_seconds, logger=logger)
        if config.cache_path
        else None
    )
    resolver = AIResolver(
        provider,
        config,
        cache=cache,
        rules_version=stable_file_version(args.rules),
        environ=environ,
        logger=logger,
    )
    return resolver, config


def _classify_items(args, resolver, size_report, environ):
    # type: (argparse.Namespace, object, object, object) -> tuple
    """Classify measured items; never let AI setup errors abort the scan."""
    if resolver is None or size_report is None:
        return None, None
    try:
        engine = load_engine(
            args.rules,
            args.catalog,
            environ=environ,
            ai_resolver=resolver,
        )
    except RulesError as exc:
        print("rules error: %s" % exc, file=sys.stderr)
        print("AI 分析未启用：规则数据不可用，已按原逻辑继续。", file=sys.stderr)
        return None, None
    classifications = [engine.classify(item) for item in size_report.items]
    return classifications, resolver.stats()


def _ai_payload(size_report, classifications, ai_stats):
    # type: (object, list, dict) -> dict
    """Machine-readable AI section: stats, safety counts and suggestions."""
    summary = {SAFETY_GREEN: 0, SAFETY_YELLOW: 0, SAFETY_RED: 0}
    suggestions = []
    for item, classification in zip(size_report.items, classifications):
        summary[classification.safety] = summary.get(classification.safety, 0) + 1
        if classification.source != SOURCE_AI:
            continue
        suggestions.append(
            {
                "item_id": classification.item_id,
                "tool_id": item.tool_id,
                "rel_path": item.rel_path,
                "safety": classification.safety,
                "source": classification.source,
                "reason": classification.reason,
                "requires_confirm": classification.requires_confirm,
                "can_migrate": classification.can_migrate,
            }
        )
    return {
        "stats": ai_stats,
        "summary": summary,
        "suggestions": suggestions,
    }


def _format_bytes(value):
    # type: (int) -> str
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return "%d %s" % (int(size), unit)
            return "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%d B" % value


def _print_human(result, catalog_path, verbose, size_report, process_hits,
                 classifications=None, ai_stats=None):
    summary = result.summary()
    tools = summary["tools"]
    directories = summary["directories"]
    print("catalog: %s" % catalog_path)
    print(
        "tools: {total} total, {present} present, {absent} absent, "
        "{no_permission} no_permission".format(**tools)
    )
    print(
        "directories: {total} total, {present} present, {absent} absent, "
        "{no_permission} no_permission".format(**directories)
    )
    print("warnings: %d" % summary["warnings"]["total"])
    for tool in result.tools:
        print("  %-14s %-22s %s" % (tool.status, tool.id, tool.name))
        if verbose:
            for hit in tool.directories:
                print(
                    "      %-14s %-5s %-5s %s"
                    % (hit.status, hit.type, hit.safety, hit.pattern)
                )
                for path in hit.resolved:
                    print("          -> %s" % path)

    if size_report is not None:
        totals = size_report.summary()
        print(
            "sizes: {items} items, {files} files, {bytes} bytes "
            "({human}), {access_errors} access errors".format(
                human=_format_bytes(totals["bytes"]), **totals
            )
        )
        per_tool = {}
        for item in size_report.items:
            key = item.tool_id or "(adhoc)"
            entry = per_tool.setdefault(key, [0, 0])
            entry[0] += item.bytes
            entry[1] += item.file_count
        for tool_id in sorted(per_tool):
            total_bytes, total_files = per_tool[tool_id]
            print(
                "  %-22s %10s  %8d files"
                % (tool_id, _format_bytes(total_bytes), total_files)
            )
        if verbose:
            for item in size_report.items:
                print(
                    "      %-14s %10s %6d files  %s"
                    % (
                        item.kind,
                        _format_bytes(item.bytes),
                        item.file_count,
                        item.source_path,
                    )
                )
        for warning in size_report.warnings:
            print(
                "      warning %-18s %s" % (warning.code, warning.path or "")
            )

    if process_hits is not None:
        counts = summarize_processes(process_hits)
        print(
            "processes: {tools} tools, {running} running, {not_running} "
            "not_running, {no_process_names} without names, {unknown} unknown "
            "({blocked} blocked)".format(**counts)
        )
        for tool_id in sorted(process_hits):
            hit = process_hits[tool_id]
            pids = ",".join(str(pid) for pid in hit.pids) or "-"
            print(
                "  %-22s %-18s pids=%s"
                % (tool_id, hit.status, pids)
            )

    if ai_stats is not None:
        print(
            "ai: {calls} calls, {cache_hits} cache hits, {suggestions} "
            "suggestions, {failures} failures, {budget_skips} budget skips, "
            "{estimated_tokens} tokens (max {max_calls} calls)".format(**ai_stats)
        )
        if classifications is not None and size_report is not None:
            counts = {SAFETY_GREEN: 0, SAFETY_YELLOW: 0, SAFETY_RED: 0}
            for classification in classifications:
                counts[classification.safety] = counts.get(classification.safety, 0) + 1
            print(
                "    green %d, yellow %d, red %d"
                % (counts[SAFETY_GREEN], counts[SAFETY_YELLOW], counts[SAFETY_RED])
            )
            for item, classification in zip(size_report.items, classifications):
                if classification.source != SOURCE_AI:
                    continue
                suffix = " (requires confirmation)" if classification.requires_confirm else ""
                print(
                    "    %-6s %-22s %s: %s%s"
                    % (
                        classification.safety,
                        item.tool_id,
                        item.rel_path,
                        classification.reason,
                        suffix,
                    )
                )


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        catalog = load_catalog(args.catalog)
    except CatalogError as exc:
        print("catalog error: %s" % exc, file=sys.stderr)
        return 1

    wanted = set(args.tool) if args.tool else None
    selected_catalog_tools = [
        tool for tool in catalog.tools
        if wanted is None or tool.id in wanted
    ]

    result = discover(catalog)

    if wanted is not None:
        result.tools = [tool for tool in result.tools if tool.id in wanted]
        result.warnings = [
            warning
            for warning in result.warnings
            if warning.tool_id is None or warning.tool_id in wanted
        ]

    size_report = None
    if args.sizes:
        size_report = measure(targets_from_discovery(result))

    process_hits = None
    if args.processes:
        process_hits = detect_processes(selected_catalog_tools)

    classifications = None
    ai_stats = None
    if args.ai:
        if size_report is None:
            print(AI_HINT_NEEDS_SIZES, file=sys.stderr)
        else:
            resolver, _ai_config = _build_ai_resolver(args, os.environ)
            classifications, ai_stats = _classify_items(
                args, resolver, size_report, os.environ
            )

    if args.json:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
        payload = result.to_dict()
        if size_report is not None:
            payload["sizes"] = size_report.to_dict()
        if classifications is not None:
            payload["ai"] = _ai_payload(size_report, classifications, ai_stats)
        if process_hits is not None:
            payload["processes"] = {
                "summary": summarize_processes(process_hits),
                "tools": [
                    process_hits[tool_id].to_dict()
                    for tool_id in sorted(process_hits)
                ],
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_human(
            result,
            args.catalog,
            args.verbose,
            size_report,
            process_hits,
            classifications=classifications,
            ai_stats=ai_stats,
        )

    # A completed scan with absent/no_permission entries is still a success.
    return 0


if __name__ == "__main__":
    sys.exit(main())
