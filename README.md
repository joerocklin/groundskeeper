# Groundskeeper

## On the use of AI

A lot in this project was AI generated, but using good software engineering practices. There are a lot of different opinions on the acceptable use of AI. If your view excludes you from using this project: I am sorry. There are many places to discuss the ethics of AI usage, but this project is not the place.

This section was hand-written[^hand-written] by a human.

[^hand-written]: To be fair, it was typed. In a text editor. That text editor was showing me spelling errors as I typed.

## Sharing your map in a bug report

Zone geometry in diagnostics, debug recordings and bug reports is real GPS data for your home. Before you attach it, run it through the anonymizer:

```
python3 devtools/anonymize_geojson.py map.geojson -o map.anon.geojson
```

It moves the whole map to a random place on Earth without rotating or scaling it, so every cardinal bearing, distance and shape is preserved and Zone bearing and angle-range behaviour reproduce exactly. It also replaces device-specific ids and drops altitude and device or account properties. The destination is random and not derived from your coordinates. The shape and size of your lawn are kept on purpose, so redraw or crop the outline if it is distinctive. See the notes at the top of `devtools/anonymize_geojson.py`.
