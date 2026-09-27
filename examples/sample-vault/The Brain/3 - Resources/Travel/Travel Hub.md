# Travel Hub & Destination Matrix

Interactive destination matrix for saved travel media. Cards carry Map View
fields (`location`, `location_name`, `weight`, `mapMarkerColor`). Pin color
follows weight: grey (0) → blue (1–2) → orange (3–5) → red (6+).

Triage cards here: set `status` to `kept`, `promoted`, or `tossed` in each
card's frontmatter. Run `second-brain ledger-sync` before deleting tossed
cards so they are never re-ingested.

## 1. Hotspots & Frequent Recommendations

```dataview
TABLE
  location_name as "Location",
  weight as "References",
  summary as "Core Idea",
  creator as "Curator",
  status as "Status"
FROM "3 - Resources/Travel"
WHERE file.name != this.file.name AND status != "tossed"
SORT weight desc, file.ctime desc
```

## 2. Location Synthesis & Cluster Notes

*Group recurring recommendations by destination before promoting them to active itineraries:*

- **Tokyo:** [[Hidden ramen alley in Shibuya]]
- **New York:**
- **Other:**

## 3. Trip Promotion Workflow

When planning a specific trip:

1. Create `1 - Projects/Trip - [Destination]`.
2. Move or transclude relevant cards from `3 - Resources/Travel/` into the project itinerary (`![[Card Name]]`).
3. Set card status to `status: "promoted"`.
