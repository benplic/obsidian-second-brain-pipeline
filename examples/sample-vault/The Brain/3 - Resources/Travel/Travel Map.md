# Travel Map

Embedded [Map View](https://github.com/esm7/obsidian-map-view) of notes under
`3 - Resources/Travel`. Pins appear once a card's `location` is set to
`[lat, lng]` (or `"lat,lng"`). Marker color follows `mapMarkerColor` / weight.

Requires the **Map View** community plugin. Until enrichment geocodes places,
the map may be empty even though cards carry the schema.

```mapview
{"name":"Travel Destinations","query":"path:\"3 - Resources/Travel\"","autoFit":true,"embeddedHeight":600}
```

See also: [[Travel Hub]]
