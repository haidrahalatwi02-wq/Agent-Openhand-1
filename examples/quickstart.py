"""Example: use the Lead Finder Agent as a library.

Runs entirely offline using the bundled `sample` provider, so it is safe to run
with no network and no API keys:

    python examples/quickstart.py

The script shows the three things most integrations need:

1. Build an ``AgentContext`` once and reuse it.
2. Run the Lead Finder agent for a search.
3. Read results back and export them.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from lead_finder_agent.checker.http_checker import HttpWebsiteChecker
from lead_finder_agent.core.agent import AgentContext, LeadFinderAgent
from lead_finder_agent.search.registry import build_providers
from lead_finder_agent.storage.sqlite_repository import SQLiteLeadRepository
from lead_finder_agent.utils.http import HttpClient, HttpResponse


def offline_checker() -> HttpWebsiteChecker:
    """A website checker that never touches the network.

    Real integrations should omit this and let the agent build the default HTTP
    checker. It exists here so the example is deterministic.
    """

    def transport(method, url, params, data, headers, timeout) -> HttpResponse:
        return HttpResponse(0, "", {}, url, error="example: network disabled")

    return HttpWebsiteChecker(client=HttpClient(transport=transport), probe_by_name=False)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "example.db"

        # 1. One context, shared by every agent you create.
        context = AgentContext(
            repository=SQLiteLeadRepository(db_path),
            providers=build_providers(["sample"]),
            checker=offline_checker(),
        )

        # 2. Run the agent.
        agent = LeadFinderAgent(context=context)
        result = agent.run(city="Aden", limit=10)

        print(f"Found {result.count} lead(s) for {result.query.city}\n")
        print(f"{'Business':<32}{'Website':<20}{'Score':>6}  {'Priority'}")
        print("-" * 70)
        for lead in result.top(10):
            print(
                f"{lead.business_name[:30]:<32}"
                f"{str(lead.website_status):<20}"
                f"{lead.lead_score:>6}  {lead.priority}"
            )

        # 3. Read stored data back and export it.
        print(f"\nStored in the database: {agent.stats()['total']}")

        csv_path = Path(tmp) / "leads.csv"
        agent.export(str(csv_path), fmt="csv", min_score=60)
        print(f"Exported the shortlist to {csv_path.name}")

        context.close()


if __name__ == "__main__":
    main()