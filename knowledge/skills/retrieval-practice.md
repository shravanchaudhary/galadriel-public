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

## Practice

1. **Read** the candidate material (turn, notes, or draft lesson).
2. **Dig deep:** `palace_search` / `palace_kg_query` for related neighbors;
   decide room (`knowledge` vs `episodes`) and whether a KG triple fits.
3. **Encode:** `palace_add_drawer` / `palace_kg_add` (invalidate+add if a fact
   changed) / optional lean `MEMORY.md`; for generalizable procedures, also a
   compact `knowledge/` entry plus `knowledge/INDEX.md` row.
4. **Recite/test:** query palace/KG again with a natural question as if you
   did not just write it; note anything you cannot retrieve.
5. **Review:** patch the store for gaps; create or patch `learn_recall` with
   quality cues pointing at that store (positives = realistic phrasings;
   lexical = high-precision anchors; negatives = near-misses; instruction =
   one-liner pointer).
6. **Spaced retest (reflection):** pick one already-known palace/KG item (or
   exercise an existing recall against recent `get_recent_recalls`); retrieve
   it; strengthen or leave cues only if the retrieve fails or cues misfire.
   Do not skip familiar material.
