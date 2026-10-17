"""Model C: turn Music, Fitness, and Recipe saves into usable notes.

This track does not replace Steps 1-3 or the crusher. It reads its own queue
(``model_c_pending.txt``) so it does not race the main pipeline or Model A.
Aggregation happens in the Dataview hubs, not in a combined digest file.
"""
