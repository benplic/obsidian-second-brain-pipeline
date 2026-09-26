"""Model A: an experimental, deeper-analysis track for high-value categories.

The main pipeline sorts everything into 13 folders cheaply (100 items per
request). Model A focuses on Tech & Coding, Project Ideas and Movies & Shows,
scores confidence, routes ambiguous items to ``Inbox/Manual Review``, adds
criteria tags (genre, tool, project time...) and feeds the Movies Kanban.

* ``ingest``  - per-URL capture with tiered extraction (metadata -> audio
  transcript -> keyframes). Costs 1-3 Gemini requests per URL, so it does not
  fit the 20/day free tier for bulk use.
* ``enrich``  - retroactively re-classifies cards already in the vault, 10 per
  request.

Neither is part of ``run_pipeline.bat``. Tier 3 (keyframes) needs the optional
``model-a`` extra (opencv-python, pillow).
"""
