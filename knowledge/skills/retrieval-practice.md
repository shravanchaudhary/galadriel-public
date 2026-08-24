# Retrieval practice

**Trigger:** Filing durable knowledge, the user is teaching something to reuse,
a reflection retro, or you are about to claim a past fact.

**Palace:** `palace_search(query="retrieval practice 3R dig deep spaced retest", room="knowledge")`

## Orientation

Passive rereading and chat re-dumps do not stick. Learning that later streams
can use is: dig deep into what you already know, encode into the right store,
retrieve-test without the write buffer, restudy gaps, and periodically retest
items you already know. Semantic recalls stay short when-to-recollect pointers
at that store — never essays of the fact.

## During a task

1. **Read** the candidate material (turn, notes, or draft lesson).
2. **Dig deep:** `palace_search` / `palace_kg_query` for related neighbors, so
   you encode next to what you already know instead of beside it.
3. **Encode:** one `learn` call with an explicit `type` — `semantic` (content
   for prose, `kg_triplets` for entity facts), `procedural` for a reusable
   how-to, `preference` for how to behave. Pass `topic`: it becomes the hall,
   which is what puts this memory next to its neighbors.
4. **Recite/test:** query palace/KG again with a natural question, phrased as
   if you had not just written it. If you cannot retrieve what you just filed,
   the content was written for the writer, not the reader — file a clearer
   version rather than leaving it.

Stop there. Steps 5 and 6 belong to the consolidation passes.

## During consolidation

5. **Trigger:** a stored memory nobody can recall on cue is inert. Give it a
   `learn_recall` pointer with quality cues (positives = realistic phrasings;
   lexical = high-precision anchors; negatives = near-misses; instruction =
   a one-liner pointer, never an essay of the fact itself).
6. **Spaced retest:** pick an already-known palace/KG item, or exercise an
   existing recall against recent `get_recent_recalls`; retrieve it; strengthen
   or leave cues only if the retrieve fails or the cues misfire. Do not skip
   familiar material — the point is catching decay in things you assume are
   solid.
