# large-text-on-disk

**Trigger:** A tool result, page, log, dump, or document is large — or you
only need one part of it — and reading it whole would spend the context on
text you will throw away.

**Rule:** Put the text on disk first, understand it with a survey, then slice
and clean it with tools that never route the bulk through the conversation.
Never read a big file to rewrite a cleaned copy of it from your reply.

**Steps:**
1. Get it on disk. Anything over the inline bound spills there on its own;
   otherwise pass `save_to=<path>` (`fetch_url_data(url, mode="raw",
   save_to=...)`, `browser("get html", save_to=...)`, `run_shell(cmd,
   save_to=...)`). The reply is the file's survey, not the text.
2. Orient: read the survey — head, tail, evenly spaced probes with token
   offsets and lines. Name what each region is (markup, data, the part you
   want).
3. Locate: `run_shell("grep -n -o '<marker>' file | head")` and `grep -c` to
   find and count the structure you want; or `survey_file(path, start, end)`
   between two probes to zoom.
4. Extract on disk: one `run_shell` python or shell command that parses the
   file (`html.parser` / `bs4` / `lxml` for markup, `json`, `re`, `awk`,
   `sed`) and writes the clean result to a NEW file. Keep the original.
5. Verify the derivative: `survey_file(new_path)`, `wc -l`, a spot-check
   `read_file(new_path, start, end)`. Iterate the extraction, not the reading.
6. Use it: `read_file` windows for detail, `study_file` if you will search it
   by meaning, `learn` for the few facts that must hold later.

Worked shape (a page whose useful content sits inside a large markup shell):
`fetch_url_data(url, mode="raw", save_to="state/work/page.html")` →
`grep -c '<segment-tag'` → `python3 -c "from bs4 import BeautifulSoup; ..."`
writing `state/work/page.clean.txt` → `survey_file` on the clean file. Total
cost: a few surveys and one grep, regardless of the page's size.

**Palace:** `palace_search("large text survey slice clean on disk", room="knowledge")`
