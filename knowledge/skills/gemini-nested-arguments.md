# gemini-nested-arguments

**Trigger:** Tool/filter arguments arriving from Gemini as protobuf `Struct`
values break nested PyMongo filters or look like opaque objects.

**Rule:** Recursively convert protobuf Struct values to native dictionaries
before passing nested filters to PyMongo (or any code expecting plain dicts).

**Steps:**
1. Detect Struct / MapComposite-like nested values in tool input.
2. Recursively convert to plain `dict` / list / scalars.
3. Only then call PyMongo / downstream helpers.

**Palace:** `palace_search("Gemini protobuf Struct nested dictionary", room="knowledge")`
