"""Core API client for iNaturalist."""

import requests
from typing import Dict, List, Optional, Any
from .exceptions import iNatAPIError, PlaceNotFoundError, TaxonNotFoundError


class iNatClient:
    """Client for interacting with the iNaturalist API."""

    BASE_URL = "https://api.inaturalist.org/v1"

    def __init__(self, user_agent: str = "inat-diff/0.1.0"):
        """Initialize the client with optional user agent."""
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json"
        })

    def _make_request(self, endpoint: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """Make a request to the iNaturalist API."""
        url = f"{self.BASE_URL}/{endpoint}"
        try:
            response = self.session.get(url, params=params or {})
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            raise iNatAPIError(f"API request failed: {e}")

    def search_places(self, query: str, place_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Search for places by name."""
        params = {"q": query}
        if place_type:
            params["place_type"] = place_type

        result = self._make_request("places/autocomplete", params)
        return result.get("results", [])

    def get_place(self, place_id: int) -> Dict[str, Any]:
        """Get detailed information about a specific place."""
        result = self._make_request(f"places/{place_id}")
        return result.get("results", [{}])[0]

    def get_place_by_id(self, place_id: int) -> Optional[Dict[str, Any]]:
        """
        Get place information by ID, returning a consistent info dict.

        Returns:
            dict with id, name, display_name, place_type or None if not found
        """
        try:
            place = self.get_place(place_id)
            if not place or not place.get("id"):
                return None
            return {
                "id": place.get("id"),
                "name": place.get("name"),
                "display_name": place.get("display_name"),
                "place_type": place.get("place_type"),
            }
        except Exception:
            return None

    def search_taxa(self, query: str, rank: Optional[str] = None) -> List[Dict[str, Any]]:
        """Search for taxa by name."""
        params = {"q": query}
        if rank:
            params["rank"] = rank

        result = self._make_request("taxa", params)
        return result.get("results", [])

    def get_observations(self,
                        place_id: Optional[int] = None,
                        taxon_id: Optional[int] = None,
                        taxon_name: Optional[str] = None,
                        d1: Optional[str] = None,
                        d2: Optional[str] = None,
                        per_page: int = 200,
                        page: int = 1,
                        **kwargs) -> Dict[str, Any]:
        """Get observations with various filters."""
        params = {
            "per_page": per_page,
            "page": page
        }

        if place_id:
            params["place_id"] = place_id
        if taxon_id:
            params["taxon_id"] = taxon_id
        if taxon_name:
            params["taxon_name"] = taxon_name
        if d1:
            params["d1"] = d1
        if d2:
            params["d2"] = d2

        # Add any additional parameters
        params.update(kwargs)

        return self._make_request("observations", params)

    def get_species_counts(self,
                          place_id: Optional[int] = None,
                          taxon_id: Optional[int] = None,
                          d1: Optional[str] = None,
                          d2: Optional[str] = None,
                          iconic_taxon: Optional[str] = None,
                          per_page: int = 500,
                          page: int = 1,
                          **kwargs) -> Dict[str, Any]:
        """
        Get species counts - much more efficient than fetching all observations.

        This endpoint returns aggregated counts of species/taxa without fetching
        individual observations. Uses leaf_taxa=true to include all taxonomic ranks
        (genus, family, etc.), not just species-level identifications.
        """
        params = {
            "per_page": per_page,
            "page": page,
            "leaf_taxa": "true"  # Include all taxonomic ranks, not just species
        }

        if place_id:
            params["place_id"] = place_id
        if taxon_id:
            params["taxon_id"] = taxon_id
        if d1:
            params["d1"] = d1
        if d2:
            params["d2"] = d2
        if iconic_taxon:
            params["iconic_taxa"] = iconic_taxon

        params.update(kwargs)

        return self._make_request("observations/species_counts", params)

    def resolve_place(self, place_name: str) -> int:
        """Resolve a place name to a place ID, preferring political boundaries."""
        places = self.search_places(place_name)

        if not places:
            raise PlaceNotFoundError(f"No places found for '{place_name}'")

        # Place type mapping from iNaturalist source code
        # https://github.com/inaturalist/inaturalist/blob/main/app/models/place.rb
        PLACE_TYPE_MAPPING = {
            12: "country",
            8: "state",
            9: "county",
            7: "town",
            103: "province",
            100: "open_space",
            # Add more mappings as needed
        }

        # Prioritize places by type (countries, states, counties)
        priority_types = ["country", "state", "county", "province"]

        for place_type in priority_types:
            for place in places:
                numeric_place_type = place.get("place_type")
                place_type_name = PLACE_TYPE_MAPPING.get(numeric_place_type, "").lower()
                if place_type_name == place_type and place_name.lower() in place.get("name", "").lower():
                    return place["id"]

        # If no priority match, return the first exact name match
        for place in places:
            if place.get("name", "").lower() == place_name.lower():
                return place["id"]

        # If no exact match, return the first result
        return places[0]["id"]

    def resolve_place_with_info(self, place_name: str) -> tuple[int, dict]:
        """
        Resolve a place name to a place ID and return detailed information.

        Returns:
            tuple: (place_id, place_info_dict) where place_info_dict contains:
                - id: The place ID
                - name: The place name
                - display_name: The full display name
                - place_type: The place type code
                - matched_as: How the place was matched (priority/exact/fallback)
        """
        places = self.search_places(place_name)

        if not places:
            raise PlaceNotFoundError(f"No places found for '{place_name}'")

        # Place type mapping from iNaturalist source code
        # https://github.com/inaturalist/inaturalist/blob/main/app/models/place.rb
        PLACE_TYPE_MAPPING = {
            12: "country",
            8: "state",
            9: "county",
            7: "town",
            103: "province",
            100: "open_space",
            # Add more mappings as needed
        }

        # Prioritize places by type (countries, states, counties)
        priority_types = ["country", "state", "county", "province"]

        for place_type in priority_types:
            for place in places:
                numeric_place_type = place.get("place_type")
                place_type_name = PLACE_TYPE_MAPPING.get(numeric_place_type, "").lower()
                if place_type_name == place_type and place_name.lower() in place.get("name", "").lower():
                    return place["id"], {
                        "id": place["id"],
                        "name": place.get("name"),
                        "display_name": place.get("display_name"),
                        "place_type": place.get("place_type"),
                        "matched_as": f"priority ({place_type})"
                    }

        # If no priority match, return the first exact name match
        for place in places:
            if place.get("name", "").lower() == place_name.lower():
                return place["id"], {
                    "id": place["id"],
                    "name": place.get("name"),
                    "display_name": place.get("display_name"),
                    "place_type": place.get("place_type"),
                    "matched_as": "exact name match"
                }

        # If no exact match, return the first result
        first_place = places[0]
        return first_place["id"], {
            "id": first_place["id"],
            "name": first_place.get("name"),
            "display_name": first_place.get("display_name"),
            "place_type": first_place.get("place_type"),
            "matched_as": "fallback (first result)"
        }

    def resolve_taxon(self, taxon_name: str) -> int:
        """Resolve a taxon name to a taxon ID."""
        taxa = self.search_taxa(taxon_name)

        if not taxa:
            raise TaxonNotFoundError(f"No taxa found for '{taxon_name}'")

        # Look for exact match first
        for taxon in taxa:
            if taxon.get("name", "").lower() == taxon_name.lower():
                return taxon["id"]

        # Return first result if no exact match
        return taxa[0]["id"]
