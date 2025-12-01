"""Main query functionality for species detection."""

from typing import List, Dict, Any, Optional
from .client import iNatClient
from .utils import parse_time_period, normalize_taxon_name
from .exceptions import iNatAPIError


class SpeciesQuery:
    """Main class for querying species observations and detecting patterns."""

    def __init__(self, client: Optional[iNatClient] = None):
        """Initialize with an optional client instance."""
        self.client = client or iNatClient()

    def query_species_in_period(self,
                               taxon_name: str,
                               time_period: str,
                               region: str,
                               include_subspecies: bool = True) -> Dict[str, Any]:
        """
        Query for species observations in a specific time period and region.

        Args:
            taxon_name: Latin name of the taxon to search for
            time_period: Time period string (e.g., "last 30 days", "this month")
            region: Name of the region (country, state, county)
            include_subspecies: Whether to include subspecies in results

        Returns:
            Dictionary containing query results and metadata
        """
        # Parse inputs
        normalized_taxon = normalize_taxon_name(taxon_name)
        start_date, end_date = parse_time_period(time_period)

        # Resolve place and taxon IDs
        place_id = self.client.resolve_place(region)
        taxon_id = self.client.resolve_taxon(normalized_taxon)

        # Get observations for the specified period
        observations = self.client.get_observations(
            place_id=place_id,
            taxon_id=taxon_id,
            d1=start_date,
            d2=end_date
        )

        # Get species information
        place_info = self.client.get_place(place_id)

        return {
            "query": {
                "taxon_name": normalized_taxon,
                "taxon_id": taxon_id,
                "region": region,
                "place_id": place_id,
                "time_period": time_period,
                "start_date": start_date,
                "end_date": end_date
            },
            "place_info": place_info,
            "observations": observations,
            "total_results": observations.get("total_results", 0),
            "per_page": observations.get("per_page", 0),
            "page": observations.get("page", 1)
        }

    def find_new_species_in_period(self,
                                  taxon_name: str,
                                  time_period: str,
                                  region: str,
                                  lookback_years: int = 20) -> Dict[str, Any]:
        """
        Find if a species has appeared in a region during a time period
        when it was not previously observed.

        Args:
            taxon_name: Latin name of the taxon to search for
            time_period: Recent time period to check
            region: Name of the region
            lookback_years: How many years to look back for historical data

        Returns:
            Dictionary with results and analysis
        """
        from datetime import datetime, timedelta

        # Get current period observations
        current_results = self.query_species_in_period(taxon_name, time_period, region)

        if current_results["total_results"] == 0:
            return {
                **current_results,
                "historical_observations": {"total_results": 0},
                "is_new_to_region": False,
                "lookback_period": "N/A",
                "analysis": "No observations found in the specified period"
            }

        # Check historical presence
        start_date, end_date = parse_time_period(time_period)
        historical_end = datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=1)
        historical_start = historical_end - timedelta(days=365 * lookback_years)

        historical_observations = self.client.get_observations(
            place_id=current_results["query"]["place_id"],
            taxon_id=current_results["query"]["taxon_id"],
            d1=historical_start.strftime("%Y-%m-%d"),
            d2=historical_end.strftime("%Y-%m-%d")
        )

        is_new = historical_observations.get("total_results", 0) == 0

        return {
            **current_results,
            "historical_observations": historical_observations,
            "is_new_to_region": is_new,
            "lookback_period": f"{historical_start.strftime('%Y-%m-%d')} to {historical_end.strftime('%Y-%m-%d')}",
            "analysis": (
                f"Species appears to be NEW to {region} in the specified period. "
                f"No observations found in the previous {lookback_years} years."
                if is_new else
                f"Species was previously observed in {region}. "
                f"Found {historical_observations.get('total_results', 0)} historical observations."
            )
        }

    def get_all_species_in_period(self,
                                 time_period: str,
                                 region: str,
                                 page_limit: Optional[int] = None) -> Dict[str, Any]:
        """
        Get all species observed in a region during a time period.

        Args:
            time_period: Time period string
            region: Name of the region
            page_limit: Maximum number of pages to fetch (None = fetch all pages)

        Returns:
            Dictionary with all species found
        """
        start_date, end_date = parse_time_period(time_period)
        place_id = self.client.resolve_place(region)

        all_observations = []
        page = 1

        while page_limit is None or page <= page_limit:
            observations = self.client.get_observations(
                place_id=place_id,
                d1=start_date,
                d2=end_date,
                page=page,
                per_page=200
            )

            results = observations.get("results", [])
            if not results:
                break

            all_observations.extend(results)
            page += 1

        # Extract unique species
        unique_species = {}
        for obs in all_observations:
            taxon = obs.get("taxon")
            if taxon and taxon.get("id"):
                species_id = taxon["id"]
                if species_id not in unique_species:
                    unique_species[species_id] = {
                        "id": species_id,
                        "name": taxon.get("name"),
                        "preferred_common_name": taxon.get("preferred_common_name"),
                        "rank": taxon.get("rank"),
                        "observation_count": 0
                    }
                unique_species[species_id]["observation_count"] += 1

        return {
            "query": {
                "region": region,
                "place_id": place_id,
                "time_period": time_period,
                "start_date": start_date,
                "end_date": end_date
            },
            "species_count": len(unique_species),
            "total_observations": len(all_observations),
            "species": list(unique_species.values())
        }

    def find_all_new_species_in_period(self,
                                       time_period: str,
                                       region: str,
                                       lookback_years: int = 20,
                                       rate_limit: float = 1.2,  # iNaturalist API limit: 60-100 req/min (https://www.inaturalist.org/pages/api+recommended+practices)
                                                                 # 1.2 seconds = 50 req/min, conservative to avoid throttling
                                       taxon_id: Optional[int] = None,
                                       verbose: bool = False) -> Dict[str, Any]:
        """
        Find all species that appear to be new to a region during a time period.

        This is the main use case: "what new species have been seen this month
        in Oregon that have not previously been observed in Oregon?"

        Uses two species_counts queries and compares them - much faster than
        checking each species individually.

        Args:
            time_period: Recent time period to check
            region: Name of the region
            lookback_years: How many years to look back for historical data
            rate_limit: Seconds to wait between API calls (default: 1.2 = 50 req/min)
            taxon_id: Optional iNaturalist taxon ID to filter results
            verbose: Print progress information

        Returns:
            Dictionary with new species found and analysis
        """
        from datetime import datetime, timedelta
        import time
        import sys

        # Parse dates
        start_date, end_date = parse_time_period(time_period)
        historical_end = datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=1)
        historical_start = historical_end - timedelta(days=365 * lookback_years)

        place_id, place_info = self.client.resolve_place_with_info(region)

        # Print place resolution info if verbose
        if verbose:
            print(f"Resolved '{region}' to:", file=sys.stderr)
            print(f"  Place: {place_info['display_name']}", file=sys.stderr)
            print(f"  Place ID: {place_info['id']}", file=sys.stderr)
            print(f"  Match type: {place_info['matched_as']}", file=sys.stderr)
            if place_info['matched_as'] == 'fallback (first result)':
                print(f"  ⚠️  WARNING: No exact match found, using first result", file=sys.stderr)
            print(file=sys.stderr)

        # Helper function to fetch all species with retry logic
        def fetch_all_species(period_start: str, period_end: str, period_name: str):
            species_map = {}
            page = 1
            no_more_results = False

            while not no_more_results:
                max_retries = 5
                retry_count = 0
                success = False

                while retry_count < max_retries and not success:
                    try:
                        counts = self.client.get_species_counts(
                            place_id=place_id,
                            taxon_id=taxon_id,
                            d1=period_start,
                            d2=period_end,
                            per_page=500,
                            page=page
                        )

                        results = counts.get("results", [])
                        if not results:
                            no_more_results = True
                            success = True
                            break

                        for taxon in results:
                            species_taxon_id = taxon.get("taxon", {}).get("id")
                            if species_taxon_id:
                                species_map[species_taxon_id] = {
                                    "id": species_taxon_id,
                                    "name": taxon.get("taxon", {}).get("name"),
                                    "preferred_common_name": taxon.get("taxon", {}).get("preferred_common_name"),
                                    "rank": taxon.get("taxon", {}).get("rank"),
                                    "iconic_taxon": taxon.get("taxon", {}).get("iconic_taxon_name"),
                                    "ancestor_ids": taxon.get("taxon", {}).get("ancestor_ids", []),
                                    "observation_count": taxon.get("count", 0)
                                }

                        success = True
                        page += 1
                        time.sleep(rate_limit)

                    except Exception as e:
                        retry_count += 1
                        if retry_count < max_retries:
                            backoff_time = rate_limit * (2 ** retry_count)
                            if verbose:
                                print(f"  Error fetching {period_name} (page {page}): {e}", file=sys.stderr)
                                print(f"  Retrying in {backoff_time:.1f}s (attempt {retry_count}/{max_retries})...", file=sys.stderr)
                            time.sleep(backoff_time)
                        else:
                            error_msg = f"Failed to fetch {period_name} (page {page}) after {max_retries} retries: {e}"
                            if verbose:
                                print(f"  {error_msg}", file=sys.stderr)
                            raise iNatAPIError(error_msg)

            return species_map

        # Step 1: Get all species in the current period
        if verbose:
            print(f"Fetching species in {region} during {time_period}...", file=sys.stderr)

        current_species_map = fetch_all_species(start_date, end_date, "current period")

        if verbose:
            print(f"Found {len(current_species_map)} species in current period", file=sys.stderr)

        # Step 2: Get all species in the historical period
        if verbose:
            print(f"Fetching historical species (lookback {lookback_years} years)...", file=sys.stderr)

        historical_species_map = fetch_all_species(
            historical_start.strftime("%Y-%m-%d"),
            historical_end.strftime("%Y-%m-%d"),
            "historical period"
        )

        if verbose:
            print(f"Found {len(historical_species_map)} species in historical period", file=sys.stderr)
            print(f"Comparing species lists (including ancestry)...", file=sys.stderr)

        # Step 3: Build a set of all historical taxon IDs that were observed
        # This includes both the taxa themselves AND any taxa that are ancestors
        # of observed taxa (to handle genus-level IDs vs species-level IDs)
        historical_taxon_ids = set(historical_species_map.keys())

        # Also build a set of all taxon IDs that appear in ancestor_ids of historical taxa
        # This helps us detect when a higher-level taxon (e.g., genus) was observed
        # in the current period but only species-level observations exist historically
        for historical_taxon in historical_species_map.values():
            historical_taxon_ids.update(historical_taxon.get("ancestor_ids", []))

        # Step 4: Compare - find species in current but NOT in historical
        new_species = []
        established_species = []

        for taxon_id, species in current_species_map.items():
            # Check if this taxon or any of its descendants were observed historically
            found_historically = False
            historical_count = 0

            # First check: exact match
            if taxon_id in historical_species_map:
                found_historically = True
                historical_count = historical_species_map[taxon_id]["observation_count"]

            # Second check: is this taxon an ancestor of any historical observations?
            # This handles the case where current period has genus-level ID but
            # historical period has species-level IDs
            elif taxon_id in historical_taxon_ids:
                found_historically = True
                # Count all historical observations of descendants
                for hist_taxon_id, hist_species in historical_species_map.items():
                    if taxon_id in hist_species.get("ancestor_ids", []):
                        historical_count += hist_species["observation_count"]

            if found_historically:
                established_species.append({
                    **species,
                    "historical_count": historical_count
                })
            else:
                new_species.append({
                    **species,
                    "historical_count": 0
                })

        if verbose:
            print(f"Complete! Found {len(new_species)} new species", file=sys.stderr)

        return {
            "query": {
                "region": region,
                "place_id": place_id,
                "place_name": place_info["name"],
                "place_display_name": place_info["display_name"],
                "place_matched_as": place_info["matched_as"],
                "time_period": time_period,
                "start_date": start_date,
                "end_date": end_date
            },
            "lookback_period": f"{historical_start.strftime('%Y-%m-%d')} to {historical_end.strftime('%Y-%m-%d')}",
            "lookback_years": lookback_years,
            "total_species_in_period": len(current_species_map),
            "new_species_count": len(new_species),
            "established_species_count": len(established_species),
            "new_species": new_species,
            "established_species": established_species,
            "rate_limit_seconds": rate_limit
        }
