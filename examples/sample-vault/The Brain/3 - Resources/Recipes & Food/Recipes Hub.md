<!-- second-brain-hub:1 -->
# Recipes Hub

Meals in this folder. `stage: ready` means the checklist was extracted.
Older cards with `status: inbox` are listed too.

```dataview
TABLE WITHOUT ID
  file.link AS "Recipe",
  creator AS "Creator",
  stage AS "Stage",
  summary AS "Summary",
  file.ctime AS "Added"
FROM "3 - Resources/Recipes & Food"
WHERE file.name != this.file.name AND status != "tossed"
SORT file.ctime DESC
```
