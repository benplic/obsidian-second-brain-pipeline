<!-- second-brain-hub:model-d:1 -->
<!--
Model D universal category hub for the Obsidian Templater plugin.
Create the note inside the category folder first (for example
"3 - Resources/Comedy/Comedy Hub.md"), then insert this template.
Templater replaces the tp.file.folder tags, including inside the Dataview fences.
-->
# <% tp.file.folder() %> Hub

Triage cards here: set `status` to `kept`, `promoted`, or `tossed`.
`stage` is pipeline state (`ready`, `to-watch`, `manual-review`) and is blank when unused.
Run `second-brain ledger-sync` before deleting tossed cards so they are never re-ingested.

## Live Inspiration Stream

```dataview
TABLE WITHOUT ID
  file.link AS "Note",
  category AS "Category",
  status AS "Status",
  summary AS "Summary",
  creator AS "Creator"
FROM "<% tp.file.folder(true) %>"
WHERE file.name != this.file.name
  AND status != "tossed"
  AND status != "manual-review"
  AND stage != "manual-review"
SORT file.ctime DESC
```

## Synthesis & Scrapbook

Recurring themes and pattern notes. Link cards with `[[Note Name]]`.

-

## Maintenance Triage Checklist

- [ ] Review notes still marked `status: inbox`
- [ ] Keep: set `status: kept` when the note stays in this category
- [ ] Promote: set `status: promoted` and move the note to `1 - Projects/<Project Name>/`
- [ ] Toss: set `status: tossed` to hide the note without breaking backlinks
- [ ] Run `second-brain ledger-sync` before deleting a tossed card

> [!warning]- Manual review
> Notes in this folder with `status: manual-review` or `stage: manual-review`.
>
> ```dataview
> TABLE WITHOUT ID
>   file.link AS "Note",
>   category AS "Category",
>   status AS "Status",
>   stage AS "Stage",
>   summary AS "Summary",
>   creator AS "Creator"
> FROM "<% tp.file.folder(true) %>"
> WHERE status = "manual-review" OR stage = "manual-review"
> SORT file.ctime DESC
> ```
