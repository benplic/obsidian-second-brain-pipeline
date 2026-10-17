# Music Hub

Unarchived tracks from the last 7 days, for batch transfer to a streaming playlist.
Set `status` to `archived` after you move a track, or `tossed` to drop it.
Run `second-brain ledger-sync` before deleting tossed cards so they are never re-ingested.

## Recent tracks

```dataview
TABLE WITHOUT ID
  file.link AS "Track",
  creator AS "Curator",
  sound AS "Sound Info",
  elink(url, "Open") AS "URL"
FROM "3 - Resources/Music"
WHERE file.name != this.file.name
  AND status != "archived"
  AND status != "tossed"
  AND file.ctime >= date(today) - dur(7 days)
SORT file.ctime DESC
```
