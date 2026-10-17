# Fitness Hub

Routines with `status: ready`. Check the movements off inside each note.

```dataview
TABLE WITHOUT ID
  file.link AS "Note",
  creator AS "Creator",
  summary AS "Summary",
  file.ctime AS "Added"
FROM "3 - Resources/Fitness & Health"
WHERE file.name != this.file.name AND status = "ready"
SORT file.ctime DESC
```
