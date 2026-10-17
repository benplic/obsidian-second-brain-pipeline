<!-- second-brain-hub:1 -->
# Fitness Hub

Workouts in this folder. `stage: ready` means the checklist was extracted.
Step 3 cards with `status: inbox` are listed too. Check movements off inside each note.

```dataview
TABLE WITHOUT ID
  file.link AS "Note",
  creator AS "Creator",
  stage AS "Stage",
  summary AS "Summary",
  file.ctime AS "Added"
FROM "3 - Resources/Fitness & Health"
WHERE file.name != this.file.name AND status != "tossed"
SORT file.ctime DESC
```
