"""Shipped catalog definitions. No network or disk writes on import."""
from typing import Any
from copy import deepcopy

# Increment only for an intentional, reviewed change to packaged starter data.
# This version is informational: startup never applies a new seed version to
# an existing mutable catalog without an explicit operator migration.
SEED_CATALOG_VERSION = 1

WIKIPEDIA_SOURCE: dict[str, Any] = {
    "source_id": "wikipedia-en",
    "title": "Wikipedia English All Maxi (2026-08)",
    "kind": "zim",
    "filename": "wikipedia_en_all_maxi_2026-08.zim",
    "source_url": "https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-08.zim",
    "sha256": "34162d18b9f96f494eac48a73a309c72e0bc95d85f76be405d0f5de795854aa6",
    "bytes": 127418087648,
    "language": "eng",
    "publisher": "Wikimedia contributors via Kiwix",
    "catalog_date": "2026-07-06",
    "license": "CC BY-SA 4.0 and GFDL for Wikipedia text; individual media licenses may vary",
    "license_url": "https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use",
    "attribution": "Preserve article-level attribution, applicable license notices, and modification notices.",
    "rights_review_status": "reviewed-for-whole-zim-redistribution",
}


# Planned sources are discovery metadata, NOT trusted acquisition manifests.
READY_GOV_SOURCE: dict[str, Any] = {
    "source_id": "fema-ready",
    "title": "FEMA / Ready.gov",
    "description": "Emergency preparedness library, English offline website archive",
    "kind": "zim",
    "filename": "ready.gov_en_all_2026-10.zim",
    "source_url": "https://download.kiwix.org/zim/zimit/ready.gov_en_all_2026-10.zim",
    "language": "eng",
    "publisher": "FEMA / Ready.gov via Kiwix",
    "edition": "2026-10",
    "provenance": "Official Kiwix ZIM download directory, checked 2026-11-20; exact size and SHA-256 not verified.",
    "availability": "candidate",
    "issue": 21,
}

# One-time compatibility registry for externally installed pre-ledger archives.
# Keep this separate from mutable seed records and the historical draft catalog
# schema: adding arbitrary fields to seeds would break strict v2 conversion.
# New ZIMs always use the normal zim_jobs lifecycle.
LEGACY_ACQUISITION_UNITS = {
    "wikipedia-en": "acquire-wikipedia-zim.service",
}

LEGACY_READER_ROUTES = {
    # Compatibility bookmark and presentation metadata for pre-ledger archives.
    # Ordinary new ZIMs use the shared /read/zim/<source-id> route.
    "wikipedia-en": {
        "path": "/wikipedia/", "entry_path": "/read/wikipedia-en",
        "name": "Wikipedia", "category": "Encyclopedia",
        "description": "Explore millions of articles, illustrations and reference material without an internet connection.",
        "symbol": "W", "language": "English",
    },
}

PLANNED_SOURCES = [
    {
        "source_id": "wikivoyage",
        "title": "Wikivoyage",
        "description": "offline travel and regional reference",
        "kind": "planned",
        "issue": 17,
        "availability": "planned"
    },
    {
        "source_id": "wikibooks",
        "title": "Wikibooks",
        "description": "offline textbooks and practical manuals",
        "kind": "planned",
        "issue": 18,
        "availability": "planned"
    },
    {
        "source_id": "epa-water",
        "title": "EPA",
        "description": "emergency drinking water and treatment guidance",
        "kind": "planned",
        "issue": 19,
        "availability": "planned"
    },
    {
        "source_id": "cdc",
        "title": "CDC",
        "description": "sanitation, hygiene and public health guidance",
        "kind": "planned",
        "issue": 20,
        "availability": "planned"
    },

    {
        "source_id": "openstreetmap",
        "title": "OpenStreetMap",
        "description": "offline maps and regional navigation",
        "kind": "planned",
        "issue": 22,
        "availability": "planned"
    },
    {
        "source_id": "hesperian",
        "title": "Hesperian Health Guides",
        "description": "community health reference",
        "kind": "planned",
        "issue": 23,
        "availability": "planned"
    },
    {
        "source_id": "red-cross",
        "title": "American Red Cross / authoritative first aid reference",
        "description": "first aid and emergency response guidance",
        "kind": "planned",
        "issue": 24,
        "availability": "planned"
    },
    {
        "source_id": "usda-preservation",
        "title": "USDA / National Center for Home Food Preservation",
        "description": "food safety and preservation",
        "kind": "planned",
        "issue": 25,
        "availability": "planned"
    },
    {
        "source_id": "usda-extension",
        "title": "USDA Extension / university extension",
        "description": "gardening and food production",
        "kind": "planned",
        "issue": 26,
        "availability": "planned"
    },
    {
        "source_id": "wiktionary",
        "title": "Wiktionary",
        "description": "offline dictionary",
        "kind": "planned",
        "issue": 27,
        "availability": "planned"
    },
    {
        "source_id": "wikisource",
        "title": "Wikisource",
        "description": "public-domain and freely licensed texts",
        "kind": "planned",
        "issue": 28,
        "availability": "planned"
    },
    {
        "source_id": "wikiquote",
        "title": "Wikiquote",
        "description": "quotations and reference",
        "kind": "planned",
        "issue": 29,
        "availability": "planned"
    },
    {
        "source_id": "wikiversity",
        "title": "Wikiversity",
        "description": "educational learning resources",
        "kind": "planned",
        "issue": 30,
        "availability": "planned"
    },
    {
        "source_id": "wikimedia-commons",
        "title": "Wikimedia Commons",
        "description": "selected offline media collections",
        "kind": "planned",
        "issue": 31,
        "availability": "planned"
    },
    {
        "source_id": "gutenberg",
        "title": "Project Gutenberg",
        "description": "offline public-domain ebook library",
        "kind": "planned",
        "issue": 32,
        "availability": "planned"
    },
    {
        "source_id": "standard-ebooks",
        "title": "Standard Ebooks",
        "description": "curated public-domain literature",
        "kind": "planned",
        "issue": 33,
        "availability": "planned"
    },
    {
        "source_id": "kolibri",
        "title": "Khan Academy / Kolibri",
        "description": "offline education",
        "kind": "planned",
        "issue": 34,
        "availability": "planned"
    },
    {
        "source_id": "openstax",
        "title": "OpenStax",
        "description": "open textbooks",
        "kind": "planned",
        "issue": 35,
        "availability": "planned"
    },
    {
        "source_id": "stack-exchange",
        "title": "Stack Exchange",
        "description": "technical Q&A archives",
        "kind": "planned",
        "issue": 36,
        "availability": "planned"
    },
    {
        "source_id": "linux-docs",
        "title": "Linux and open-source technical documentation",
        "description": "Linux administration and open-source software manuals",
        "kind": "planned",
        "issue": 37,
        "availability": "planned"
    },
    {
        "source_id": "ifixit",
        "title": "iFixit",
        "description": "offline repair guides",
        "kind": "planned",
        "issue": 38,
        "availability": "planned"
    },
    {
        "source_id": "practical-action",
        "title": "Practical Action / appropriate technology",
        "description": "resilient infrastructure guides",
        "kind": "planned",
        "issue": 39,
        "availability": "planned"
    },
    {
        "source_id": "noaa",
        "title": "NOAA / National Weather Service",
        "description": "weather and hazard reference",
        "kind": "planned",
        "issue": 40,
        "availability": "planned"
    },
    {
        "source_id": "amateur-radio",
        "title": "Amateur radio and emergency communications reference",
        "description": "radio operation and emergency communications guidance",
        "kind": "planned",
        "issue": 41,
        "availability": "planned"
    },
    {
        "source_id": "food-reference",
        "title": "Open Food Facts / USDA FoodData Central",
        "description": "offline food reference",
        "kind": "planned",
        "issue": 42,
        "availability": "planned"
    },
    {
        "source_id": "internet-archive",
        "title": "Internet Archive",
        "description": "curated offline reference collections",
        "kind": "planned",
        "issue": 43,
        "availability": "planned"
    }
]

def builtin_sources():
    """Return independent copies; stable IDs survive application upgrades."""
    return [dict(deepcopy(WIKIPEDIA_SOURCE), availability="supported"), deepcopy(READY_GOV_SOURCE)] + deepcopy(PLANNED_SOURCES)

def seed_catalog_manifest():
    """Return a versioned, detached catalog of proposed starter profiles."""
    return {"version": SEED_CATALOG_VERSION, "sources": builtin_sources()}

def planned_sources():
    return deepcopy(PLANNED_SOURCES)


# Download defaults for existing, unmodified planned source records.
CONFIGURED_PDFS = {
    "epa-water": {
        "format": "pdf",
        "title": "EPA — Emergency Disinfection of Drinking Water",
        "description": "Emergency drinking-water treatment guidance from the EPA Office of Water.",
        "publisher": "U.S. Environmental Protection Agency",
        "edition": "September 2017 (EPA 816-F-15-003)",
        "language": "eng",
        "source_url": "https://www.epa.gov/sites/default/files/2017-09/documents/emergency_disinfection_of_drinking_water_sept2017.pdf",
        "expected_size_bytes": "754723",
        "sha256": "f404083d00decedc267bf4265eda776bb250ccfe18c1f7b19152aa006ff727c5",
        "provenance": "Original EPA publication. Size and SHA-256 measured from the EPA download.",
    },
    "cdc": {
        "format": "pdf",
        "title": "CDC — Wash Your Hands",
        "description": "CDC handwashing fact sheet for water, sanitation and hygiene emergencies.",
        "publisher": "Centers for Disease Control and Prevention",
        "language": "eng",
        "source_url": "https://www.cdc.gov/water-emergency/media/pdfs/factsheet-wash-your-hands.pdf",
        "expected_size_bytes": "282412",
        "sha256": "afd3dbf6a5ac9e3e40c6d9ed4d3e00d0e6493a468db7c02c4f2aadbe9d394a03",
        "provenance": "Original CDC publication. Size and SHA-256 measured from the CDC download.",
    },
}
