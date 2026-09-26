# Comedy Hub

Triage cards here: set `status` to `kept`, `promoted`, or `tossed` in each card's frontmatter.
Run `second-brain ledger-sync` before deleting tossed cards so they are never re-ingested.

## Cards

```dataview
TABLE WITHOUT ID
  file.link AS "Card",
  creator AS "Creator",
  status AS "Status",
  file.ctime AS "Added"
FROM "3 - Resources/Comedy"
WHERE url AND status != "tossed"
SORT status ASC, file.ctime DESC
```

## Inbox (untriaged)

```dataview
LIST
FROM "3 - Resources/Comedy"
WHERE status = "inbox"
```

## Synthesis / Scrapbook

- Recurring bits worth remembering:
- 
