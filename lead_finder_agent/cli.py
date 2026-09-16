"""Command line interface for the Lead Finder Agent.

    lead-finder search --city Aden --type restaurants --limit 50
    lead-finder list --min-score 60
    lead-finder export --format csv --output exports/leads.csv

Built on ``argparse`` so the package has no CLI dependency. Every command is a
thin wrapper around :class:`~lead_finder_agent.core.agent.LeadFinderAgent`; all
real behaviour lives in the library.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from lead_finder_agent import __version__
from lead_finder_agent.cli_output import (
    LEAD_HEADERS,
    format_lead_detail,
    format_stats,
    lead_rows,
    render_table,
)
from lead_finder_agent.config.settings import Settings, get_settings, load_dotenv, reset_settings
from lead_finder_agent.core.agent import AgentContext, LeadFinderAgent
from lead_finder_agent.models import WebsiteStatus
from lead_finder_agent.search.registry import available_providers
from lead_finder_agent.storage.base import LeadFilter
from lead_finder_agent.utils.logging_utils import setup_logging
from lead_finder_agent.utils.text import parse_keywords


def resolve_location_filter(
    city: Optional[str], country: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Canonicalize a city/country pair used to filter stored leads.

    An explicit country is normalized but never replaced; when only a city is
    given its country is inferred, matching how ``search`` stores leads.
    """
    from lead_finder_agent.config.locations import get_location_resolver

    resolver = get_location_resolver()
    if resolver is None:
        return city, country
    canonical_city, inferred_country = resolver.resolve_city(city)
    if country:
        return canonical_city, resolver.resolve_country(country)
    return canonical_city, inferred_country


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="lead-finder",
        description="Find local businesses without a website and score them as sales leads.",
    )
    parser.add_argument("--version", action="version", version=f"lead-finder {__version__}")
    parser.add_argument(
        "--db",
        dest="db_path",
        help="Path to the SQLite database (default: LEAD_FINDER_DB_PATH or data/leads.db)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Log level: DEBUG, INFO, WARNING or ERROR",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- search ------------------------------------------------------------
    search = subparsers.add_parser("search", help="Search for businesses and score them")
    search.add_argument("--city", help="City to search in, e.g. Aden")
    search.add_argument("--country", help="Country to search in, e.g. Yemen")
    search.add_argument("--type", dest="business_type", help="Business type, e.g. restaurants")
    search.add_argument("--keywords", help="Comma separated extra keywords")
    search.add_argument("--limit", type=int, default=50, help="Maximum number of results")
    search.add_argument(
        "--providers",
        help=f"Comma separated providers (available: {', '.join(available_providers())})",
    )
    search.add_argument(
        "--no-website-check",
        action="store_true",
        help="Skip the website checking stage",
    )
    search.add_argument(
        "--no-store",
        action="store_true",
        help="Do not save the results to the database",
    )
    search.add_argument(
        "--max-checks",
        type=int,
        default=None,
        help="Cap the number of websites checked in one run",
    )
    search.add_argument(
        "--json",
        action="store_true",
        help="Print the full result as JSON instead of a table",
    )
    search.add_argument(
        "--show-stats",
        action="store_true",
        help="Print pipeline statistics after the results",
    )

    # -- list --------------------------------------------------------------
    list_cmd = subparsers.add_parser("list", help="List stored leads")
    list_cmd.add_argument("--city")
    list_cmd.add_argument("--country")
    list_cmd.add_argument("--type", dest="business_type")
    list_cmd.add_argument("--source")
    list_cmd.add_argument("--priority")
    list_cmd.add_argument(
        "--website-status",
        choices=[str(s) for s in WebsiteStatus],
        help="Filter by website status",
    )
    list_cmd.add_argument("--min-score", type=int)
    list_cmd.add_argument("--max-score", type=int)
    list_cmd.add_argument("--limit", type=int, default=25)
    list_cmd.add_argument(
        "--order-by",
        default="lead_score",
        choices=list(LeadFilter.ALLOWED_ORDER),
    )
    list_cmd.add_argument("--ascending", action="store_true")
    list_cmd.add_argument("--json", action="store_true")

    # -- show --------------------------------------------------------------
    show = subparsers.add_parser("show", help="Show one lead in detail")
    show.add_argument("lead_id", help="Lead id (or dedupe key)")

    # -- export ------------------------------------------------------------
    export = subparsers.add_parser("export", help="Export stored leads to JSON or CSV")
    export.add_argument("--format", choices=["json", "csv", "tsv"], default=None)
    export.add_argument("--output", required=True, help="Destination file path")
    export.add_argument("--city")
    export.add_argument("--country")
    export.add_argument("--type", dest="business_type")
    export.add_argument("--priority")
    export.add_argument(
        "--website-status",
        choices=[str(s) for s in WebsiteStatus],
    )
    export.add_argument("--min-score", type=int)
    export.add_argument("--limit", type=int)

    # -- stats / providers -------------------------------------------------
    subparsers.add_parser("stats", help="Show database statistics")
    subparsers.add_parser("providers", help="List available search providers")

    return parser


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #


def _context(args: argparse.Namespace) -> AgentContext:
    """Build an :class:`AgentContext` from parsed arguments."""
    settings = get_settings()
    if args.db_path:
        settings = settings.with_overrides(db_path=Path(args.db_path).expanduser().resolve())
        settings.ensure_directories()
    if args.log_level:
        settings = settings.with_overrides(log_level=args.log_level.upper())
        setup_logging(settings.log_level, force=True)
    return AgentContext(settings=settings)


def cmd_search(args: argparse.Namespace) -> int:
    context = _context(args)
    agent = LeadFinderAgent(
        context=context,
        check_websites=not args.no_website_check,
        store_results=not args.no_store,
        max_checks=args.max_checks,
    )
    result = agent.run(
        city=args.city,
        country=args.country,
        business_type=args.business_type,
        keywords=parse_keywords(args.keywords),
        limit=args.limit,
        providers=parse_keywords(args.providers) or None,
    )

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        leads = result.top(args.limit)
        if leads:
            print(render_table(LEAD_HEADERS, lead_rows(leads)))
        else:
            print("No leads found.")
        hot = sum(1 for lead in leads if str(lead.priority) == "hot")
        print(f"\n{result.count} lead(s) found, {hot} hot")
        for provider, error in result.errors.items():
            print(f"warning: provider {provider} failed: {error}", file=sys.stderr)
        for provider in result.stats.providers:
            # A failed provider already carries a skipped_reason (so the stats
            # dict explains every non-delivery). Reporting it again here would
            # print "failed" and "skipped" for the same provider, which reads as
            # two different outcomes.
            if provider.get("skipped_reason") and not provider.get("error"):
                print(
                    f"note: provider {provider['provider']} skipped: {provider['skipped_reason']}",
                    file=sys.stderr,
                )

    if args.show_stats:
        print()
        print(format_stats(result.stats.to_dict()))

    if args.no_store:
        print("note: results were not stored (--no-store)", file=sys.stderr)
    else:
        print(f"Stored in {context.settings.db_path}", file=sys.stderr)
    return 0


def _filter_from_args(args: argparse.Namespace) -> LeadFilter:
    website_status = getattr(args, "website_status", None)
    # Resolve location filters the same way ``search`` does, otherwise
    # ``list --city عدن`` would silently match nothing while
    # ``search --city عدن`` worked.
    city, country = resolve_location_filter(
        getattr(args, "city", None), getattr(args, "country", None)
    )
    return LeadFilter(
        city=city,
        country=country,
        business_type=getattr(args, "business_type", None),
        source=getattr(args, "source", None),
        priority=getattr(args, "priority", None),
        website_status=WebsiteStatus(website_status) if website_status else None,
        min_score=getattr(args, "min_score", None),
        max_score=getattr(args, "max_score", None),
        order_by=getattr(args, "order_by", "lead_score"),
        descending=not getattr(args, "ascending", False),
        limit=getattr(args, "limit", None),
    )


def cmd_list(args: argparse.Namespace) -> int:
    context = _context(args)
    repository = context.resolve_repository()
    leads = repository.find(_filter_from_args(args))

    if args.json:
        print(json.dumps([lead.to_dict() for lead in leads], ensure_ascii=False, indent=2, default=str))
        return 0
    if not leads:
        print("No stored leads match those filters.")
        return 0
    print(render_table(LEAD_HEADERS, lead_rows(leads)))
    print(f"\n{len(leads)} lead(s) shown (database total: {repository.count()})")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    context = _context(args)
    repository = context.resolve_repository()
    lead = repository.get(args.lead_id)
    if lead is None:
        print(f"No lead found with id {args.lead_id!r}", file=sys.stderr)
        return 1
    print(format_lead_detail(lead))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    context = _context(args)
    agent = LeadFinderAgent(context=context)
    filters = _filter_from_args(args)
    written = agent.export(
        args.output,
        fmt=args.format,
        city=filters.city,
        country=filters.country,
        business_type=filters.business_type,
        priority=filters.priority,
        website_status=filters.website_status,
        min_score=filters.min_score,
        limit=filters.limit,
    )
    print(f"Exported to {written}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    context = _context(args)
    repository = context.resolve_repository()
    stats = repository.stats()
    print(json.dumps(stats, indent=2))
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    from lead_finder_agent.search.registry import registry

    print("Available search providers:")
    for name in available_providers():
        try:
            provider = registry().create(name)
            print(f"  {name:10} {provider.description or '(no description)'}")
            reason = provider.unavailable_reason()
            if reason:
                print(f"             unavailable: {reason}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:10} (failed to load: {exc})")
    return 0


_HANDLERS = {
    "search": cmd_search,
    "list": cmd_list,
    "show": cmd_show,
    "export": cmd_export,
    "stats": cmd_stats,
    "providers": cmd_providers,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Returns the process exit code."""
    load_dotenv()
    reset_settings()
    parser = build_parser()
    args = parser.parse_args(argv)

    settings: Settings = get_settings()
    setup_logging(args.log_level or settings.log_level, force=bool(args.log_level))

    handler = _HANDLERS.get(args.command)
    if handler is None:  # pragma: no cover - argparse enforces this
        parser.print_help()
        return 2
    try:
        return int(handler(args) or 0)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI must not traceback at users
        print(f"error: {exc}", file=sys.stderr)
        setup_logging("DEBUG", force=True)
        import logging

        logging.getLogger("lead_finder_agent").debug("Unhandled error", exc_info=True)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
