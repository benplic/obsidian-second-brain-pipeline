# Recipes Hub

Meals with `status: ready`. Check ingredients and steps off inside each note.

```dataview
TABLE WITHOUT ID
  file.link AS "Recipe",
  creator AS "Creator",
  summary AS "Summary",
  file.ctime AS "Added"
FROM "3 - Resources/Recipes & Food"
WHERE file.name != this.file.name AND status = "ready"
SORT file.ctime DESC
```
