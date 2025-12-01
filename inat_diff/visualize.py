"""Generate HTML visualization from iNaturalist query results."""

import argparse
import json
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import requests


API_BASE_URL = "https://api.inaturalist.org/v1/observations"
QUALITY_PRIORITY = ("research", "needs_id", "casual")
QUALITY_LABELS = {
    "research": "Research Grade",
    "needs_id": "Needs ID",
    "casual": "Casual",
}
REQUEST_TIMEOUT = 10


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>iNaturalist Species Report: {title}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .header {{
            background-color: #74ac00;
            color: white;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
        }}
        .header h1 {{
            margin: 0 0 10px 0;
        }}
        .header p {{
            margin: 5px 0;
            opacity: 0.9;
        }}
        .summary {{
            background-color: white;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .summary h2 {{
            margin-top: 0;
        }}
        .stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-top: 15px;
        }}
        .stat {{
            background-color: #f8f8f8;
            padding: 15px;
            border-radius: 4px;
            border-left: 4px solid #74ac00;
        }}
        .stat-value {{
            font-size: 24px;
            font-weight: bold;
            color: #333;
        }}
        .stat-label {{
            font-size: 14px;
            color: #666;
            margin-top: 5px;
        }}
        .species-section {{
            background-color: white;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .species-section h2 {{
            margin-top: 0;
            color: #333;
        }}
        .species-table {{
            width: 100%;
            border-collapse: collapse;
        }}
        .species-table th {{
            text-align: left;
            padding: 12px 15px;
            background-color: #f8f8f8;
            border-bottom: 2px solid #ddd;
            font-weight: 600;
            color: #555;
        }}
        .species-table th.col-obs,
        .species-table th.col-quality,
        .species-table th.col-action {{
            text-align: center;
        }}
        .species-table td {{
            padding: 12px 15px;
            border-bottom: 1px solid #eee;
            vertical-align: middle;
        }}
        .species-table tr:hover {{
            background-color: #f8f8f8;
        }}
        .species-table tr:last-child td {{
            border-bottom: none;
        }}
        .col-name {{
            width: 45%;
        }}
        .col-obs {{
            width: 12%;
            text-align: center;
        }}
        .col-quality {{
            width: 18%;
            text-align: center;
        }}
        .col-action {{
            width: 25%;
            text-align: center;
        }}
        .species-name {{
            font-size: 15px;
            font-weight: bold;
            color: #333;
        }}
        .species-name-latin {{
            font-style: italic;
            color: #666;
            font-size: 13px;
            display: block;
            margin-top: 2px;
        }}
        .species-meta {{
            font-size: 12px;
            color: #888;
            margin-top: 6px;
        }}
        .species-badge {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 3px;
            font-size: 10px;
            font-weight: bold;
            margin-right: 4px;
            text-transform: uppercase;
        }}
        .badge-new {{
            background-color: #ff4444;
            color: white;
        }}
        .badge-rank {{
            background-color: #e8e8e8;
            color: #666;
        }}
        .badge-taxon {{
            background-color: #d4edda;
            color: #155724;
        }}
        .obs-count {{
            font-size: 18px;
            font-weight: bold;
            color: #74ac00;
        }}
        .obs-label {{
            font-size: 11px;
            color: #888;
            display: block;
        }}
        .quality-badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
        }}
        .quality-research {{
            background-color: #d4edda;
            color: #155724;
        }}
        .quality-needs-id {{
            background-color: #fff3cd;
            color: #856404;
        }}
        .quality-casual {{
            background-color: #e8e8e8;
            color: #666;
        }}
        .quality-unknown {{
            background-color: #f8f8f8;
            color: #999;
        }}
        .view-link {{
            display: inline-block;
            padding: 6px 14px;
            background-color: #74ac00;
            color: white;
            text-decoration: none;
            border-radius: 4px;
            font-size: 12px;
            transition: background-color 0.2s;
        }}
        .view-link:hover {{
            background-color: #5a8500;
        }}
        .footer {{
            text-align: center;
            color: #888;
            margin-top: 30px;
            font-size: 13px;
        }}
        .footer a {{
            color: #74ac00;
            text-decoration: none;
        }}
        .footer a:hover {{
            text-decoration: underline;
        }}
    </style>
</head>
<body>
    {content}
    <div class="footer">
        <p>Generated from iNaturalist data using <a href="https://github.com/yourusername/inat-diff">inat-diff</a></p>
        <p>Data from <a href="https://www.inaturalist.org">iNaturalist.org</a></p>
    </div>
</body>
</html>
"""


def _normalize_int(value: Any) -> Optional[int]:
    """Return value as int if possible, otherwise None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=512)
def _fetch_highest_quality_grade(taxon_id: int, place_id: Optional[int]) -> tuple[Optional[str], Optional[str]]:
    """Return highest available observation quality grade for a taxon.

    Returns:
        Tuple of (grade, error_message) where:
        - grade: The quality grade string ('research', 'needs_id', 'casual') or None if no observations
        - error_message: Error description if API call failed, None otherwise
    """
    base_params = {"taxon_id": taxon_id, "per_page": 1}
    if place_id is not None:
        base_params["place_id"] = place_id

    max_retries = 3
    retry_delay = 1.0  # Initial retry delay in seconds

    for grade in QUALITY_PRIORITY:
        params = dict(base_params)
        params["quality_grade"] = grade

        for attempt in range(max_retries):
            try:
                response = requests.get(
                    API_BASE_URL, params=params, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "inat-diff-visualize"}
                )
                response.raise_for_status()
                payload = response.json()

                # Success - check if this grade has results
                if payload.get("total_results"):
                    return (grade, None)
                # No results for this grade, try next grade
                break

            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    # Retry with exponential backoff
                    time.sleep(retry_delay * (2 ** attempt))
                else:
                    # Final attempt failed
                    return (None, f"API error: {str(e)}")
            except (ValueError, KeyError) as e:
                # JSON parsing error - don't retry
                return (None, f"Invalid API response: {str(e)}")

    # No observations found for any quality grade
    return (None, None)


def annotate_species_with_quality(species_list: Iterable[Dict[str, Any]], place_id: Any, rate_limit: float = 1.2) -> None:
    """Augment each species dict with its highest observation quality label.

    Note: This function modifies the species dictionaries in-place by adding a
    'highest_quality_grade_label' field to each species.

    Args:
        species_list: Iterable of species dictionaries to annotate
        place_id: iNaturalist place ID for the region
        rate_limit: Seconds to wait between API calls (default: 1.2)
    """
    normalized_place_id = _normalize_int(place_id)
    total_species = len(species_list) if hasattr(species_list, '__len__') else None

    for idx, species in enumerate(species_list, 1):
        # Progress indication
        if total_species:
            print(f"Fetching quality grades: {idx}/{total_species} species...", file=sys.stderr)
        else:
            print(f"Fetching quality grades: {idx} species...", file=sys.stderr)

        taxon_id = _normalize_int(species.get("id"))
        if taxon_id is None:
            species["highest_quality_grade_label"] = "Unknown"
            continue

        grade_key, error_msg = _fetch_highest_quality_grade(taxon_id, normalized_place_id)

        if error_msg:
            # API call failed
            species["highest_quality_grade_label"] = "API Error"
            species_name = species.get("name", f"taxon {taxon_id}")
            print(f"Error fetching quality for {species_name}: {error_msg}", file=sys.stderr)
        elif grade_key:
            # Successfully found quality grade
            species["highest_quality_grade_label"] = QUALITY_LABELS.get(grade_key, grade_key.title())
        else:
            # No observations found (shouldn't normally happen)
            species["highest_quality_grade_label"] = "Unknown"

        # Rate limiting - sleep after each API call
        # (We sleep even after the last one to keep the logic simple)
        time.sleep(rate_limit)


def sort_species_by_quality(species_list: list) -> list:
    """Sort species list by quality grade (Research Grade first, then Needs ID, then Casual).

    Args:
        species_list: List of species dictionaries with 'highest_quality_grade_label' field

    Returns:
        Sorted list with Research Grade species first
    """
    quality_order = {
        "Research Grade": 0,
        "Needs ID": 1,
        "Casual": 2,
        "Unknown": 3,
        "API Error": 4,
    }

    def get_quality_key(species):
        label = species.get("highest_quality_grade_label", "Unknown")
        return quality_order.get(label, 3)

    return sorted(species_list, key=get_quality_key)


def format_species_item(species: Dict[str, Any], query: Dict[str, Any], is_new: bool = False) -> str:
    """Format a single species as HTML table row."""
    name = species.get("name", "Unknown")
    common_name = species.get("preferred_common_name")
    taxon_id = species.get("id")
    place_id = query.get("place_id")
    rank = species.get("rank", "").capitalize()
    iconic_taxon = species.get("iconic_taxon", "")
    obs_count = species.get("observation_count", 0)
    quality_label = species.get("highest_quality_grade_label")

    # Build display name - common name on top, Latin name below
    if common_name:
        display_name = f'{common_name}<span class="species-name-latin">{name}</span>'
    else:
        display_name = f'<span style="font-style: italic;">{name}</span>'

    # Build observation link
    obs_link = f"https://www.inaturalist.org/observations?place_id={place_id}&taxon_id={taxon_id}"

    # Build badges
    badges = []
    if is_new:
        badges.append('<span class="species-badge badge-new">New</span>')
    if rank:
        badges.append(f'<span class="species-badge badge-rank">{rank}</span>')
    if iconic_taxon:
        badges.append(f'<span class="species-badge badge-taxon">{iconic_taxon}</span>')

    badges_html = "".join(badges)
    meta_html = f'<div class="species-meta">{badges_html}</div>' if badges_html else ""

    # Build quality badge with appropriate styling
    quality_html = ""
    if quality_label:
        quality_class = "quality-unknown"
        if quality_label == "Research Grade":
            quality_class = "quality-research"
        elif quality_label == "Needs ID":
            quality_class = "quality-needs-id"
        elif quality_label == "Casual":
            quality_class = "quality-casual"
        quality_html = f'<span class="quality-badge {quality_class}">{quality_label}</span>'

    return f"""
        <tr>
            <td class="col-name">
                <div class="species-name">{display_name}</div>
                {meta_html}
            </td>
            <td class="col-obs">
                <span class="obs-count">{obs_count:,}</span>
                <span class="obs-label">observations</span>
            </td>
            <td class="col-quality">{quality_html}</td>
            <td class="col-action">
                <a href="{obs_link}" class="view-link">View on iNaturalist</a>
            </td>
        </tr>"""


def generate_new_species_html(data: Dict[str, Any], include_quality: bool = False, rate_limit: float = 1.2) -> str:
    """Generate HTML for new-species command output."""
    query = data.get("query", {})
    region = query.get("region", "Unknown Region")
    time_period = query.get("time_period", "")
    start_date = query.get("start_date", "")
    end_date = query.get("end_date", "")

    lookback_years = data.get("lookback_years", 0)
    lookback_period = data.get("lookback_period", "")

    total_species = data.get("total_species_in_period", 0)
    new_count = data.get("new_species_count", 0)
    established_count = data.get("established_species_count", 0)

    new_species = data.get("new_species", [])
    established_species = data.get("established_species", [])

    # Build header
    title = f"New Species in {region}"
    header = f"""
    <div class="header">
        <h1>New Species Report: {region}</h1>
        <p><strong>Period:</strong> {time_period} ({start_date} to {end_date})</p>
        <p><strong>Lookback:</strong> {lookback_years} years ({lookback_period})</p>
    </div>
    """

    # Build summary stats
    summary = f"""
    <div class="summary">
        <h2>Summary</h2>
        <div class="stats">
            <div class="stat">
                <div class="stat-value">{total_species:,}</div>
                <div class="stat-label">Total Species in Period</div>
            </div>
            <div class="stat">
                <div class="stat-value">{new_count:,}</div>
                <div class="stat-label">New Species (No Historical Obs.)</div>
            </div>
            <div class="stat">
                <div class="stat-value">{established_count:,}</div>
                <div class="stat-label">Established Species</div>
            </div>
        </div>
    </div>
    """

    # Build new species table
    new_species_html = ""
    if new_species:
        if include_quality:
            annotate_species_with_quality(new_species, query.get("place_id"), rate_limit=rate_limit)
            # Sort by quality grade (Research Grade first)
            new_species = sort_species_by_quality(new_species)
        species_rows = [format_species_item(sp, query, is_new=True) for sp in new_species]
        new_species_html = f"""
        <div class="species-section">
            <h2>New Species ({new_count:,})</h2>
            <p>Species observed in {region} during {time_period} with no observations in the previous {lookback_years} years.</p>
            <table class="species-table">
                <thead>
                    <tr>
                        <th class="col-name">Species</th>
                        <th class="col-obs">Count</th>
                        <th class="col-quality">Quality</th>
                        <th class="col-action">Link</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(species_rows)}
                </tbody>
            </table>
        </div>
        """

    content = header + summary + new_species_html
    return HTML_TEMPLATE.format(title=title, content=content)


def generate_list_species_html(data: Dict[str, Any], include_quality: bool = False, rate_limit: float = 1.2) -> str:
    """Generate HTML for list-species command output."""
    query = data.get("query", {})
    region = query.get("region", "Unknown Region")
    time_period = query.get("time_period", "")
    start_date = query.get("start_date", "")
    end_date = query.get("end_date", "")

    species_count = data.get("species_count", 0)
    total_observations = data.get("total_observations", 0)
    species = data.get("species", [])

    # Build header
    title = f"Species in {region}"
    header = f"""
    <div class="header">
        <h1>Species List: {region}</h1>
        <p><strong>Period:</strong> {time_period} ({start_date} to {end_date})</p>
    </div>
    """

    # Build summary
    summary = f"""
    <div class="summary">
        <h2>Summary</h2>
        <div class="stats">
            <div class="stat">
                <div class="stat-value">{species_count:,}</div>
                <div class="stat-label">Unique Species</div>
            </div>
            <div class="stat">
                <div class="stat-value">{total_observations:,}</div>
                <div class="stat-label">Total Observations</div>
            </div>
        </div>
    </div>
    """

    # Build species table
    species_html = ""
    if species:
        if include_quality:
            annotate_species_with_quality(species, query.get("place_id"), rate_limit=rate_limit)
            # Sort by quality grade (Research Grade first)
            species = sort_species_by_quality(species)
        species_rows = [format_species_item(sp, query, is_new=False) for sp in species]
        species_html = f"""
        <div class="species-section">
            <h2>All Species ({species_count:,})</h2>
            <table class="species-table">
                <thead>
                    <tr>
                        <th class="col-name">Species</th>
                        <th class="col-obs">Count</th>
                        <th class="col-quality">Quality</th>
                        <th class="col-action">Link</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(species_rows)}
                </tbody>
            </table>
        </div>
        """

    content = header + summary + species_html
    return HTML_TEMPLATE.format(title=title, content=content)


def generate_query_html(data: Dict[str, Any]) -> str:
    """Generate HTML for query command output."""
    query = data.get("query", {})
    taxon_name = query.get("taxon_name", "Unknown")
    region = query.get("region", "Unknown Region")
    time_period = query.get("time_period", "")
    start_date = query.get("start_date", "")
    end_date = query.get("end_date", "")
    total_results = data.get("total_results", 0)

    is_new = data.get("is_new_to_region")
    analysis = data.get("analysis", "")

    # Build header
    title = f"{taxon_name} in {region}"
    header = f"""
    <div class="header">
        <h1>Species Query: <em>{taxon_name}</em></h1>
        <p><strong>Region:</strong> {region}</p>
        <p><strong>Period:</strong> {time_period} ({start_date} to {end_date})</p>
    </div>
    """

    # Build summary
    new_badge = ""
    if is_new is True:
        new_badge = '<span class="species-badge badge-new">New to Region</span>'

    summary = f"""
    <div class="summary">
        <h2>Results {new_badge}</h2>
        <div class="stats">
            <div class="stat">
                <div class="stat-value">{total_results:,}</div>
                <div class="stat-label">Observations Found</div>
            </div>
        </div>
        {f'<p style="margin-top: 15px;"><strong>Analysis:</strong> {analysis}</p>' if analysis else ''}
    </div>
    """

    # Build observation link
    place_id = query.get("place_id")
    taxon_id = query.get("taxon_id")
    obs_link = f"https://www.inaturalist.org/observations?place_id={place_id}&taxon_id={taxon_id}"

    link_section = f"""
    <div class="species-section">
        <h2>View Observations</h2>
        <p>
            <a href="{obs_link}" class="view-link">View all observations on iNaturalist</a>
        </p>
    </div>
    """

    content = header + summary + link_section
    return HTML_TEMPLATE.format(title=title, content=content)


def generate_html(data: Dict[str, Any], include_quality: bool = False, rate_limit: float = 1.2) -> str:
    """Generate HTML based on the type of query results."""
    # Detect result type based on fields present
    if "new_species_count" in data:
        return generate_new_species_html(data, include_quality=include_quality, rate_limit=rate_limit)
    elif "species_count" in data and "species" in data:
        return generate_list_species_html(data, include_quality=include_quality, rate_limit=rate_limit)
    elif "query" in data and "taxon_name" in data.get("query", {}):
        return generate_query_html(data)
    else:
        raise ValueError("Unknown JSON format - cannot determine query type")


def main():
    """CLI entry point for visualization."""
    parser = argparse.ArgumentParser(
        description="Generate HTML visualization from iNaturalist query results",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Visualize new species results
  inat-diff-visualize results.json output.html

  # Generate from list-species output
  inat-diff-visualize species_list.json species.html
        """
    )

    parser.add_argument("input_file", help="Input JSON file from inat-diff")
    parser.add_argument("output_file", help="Output HTML file")
    parser.add_argument(
        "--include-quality",
        action="store_true",
        help="Include observation quality grade for each species (requires API calls, slow for large datasets)"
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=1.2,
        dest="rate_limit",
        help="Seconds to wait between API calls when using --include-quality (default: 1.2 = 50/min, iNat limit is 60-100/min)"
    )

    args = parser.parse_args()

    # Read input JSON
    try:
        input_path = Path(args.input_file)
        if not input_path.exists():
            print(f"Error: Input file '{args.input_file}' not found", file=sys.stderr)
            sys.exit(1)

        with open(input_path, 'r') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in '{args.input_file}': {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error reading '{args.input_file}': {e}", file=sys.stderr)
        sys.exit(1)

    # Generate HTML
    try:
        html = generate_html(data, include_quality=args.include_quality, rate_limit=args.rate_limit)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error generating HTML: {e}", file=sys.stderr)
        sys.exit(1)

    # Write output
    try:
        output_path = Path(args.output_file)
        with open(output_path, 'w') as f:
            f.write(html)
        print(f"HTML report generated: {args.output_file}")
    except Exception as e:
        print(f"Error writing '{args.output_file}': {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
