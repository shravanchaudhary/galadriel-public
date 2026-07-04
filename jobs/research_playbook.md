# Playbook: Web Research — dorking, profiles & personas

*Reference for the web-fallback side of `jobs/lead_sourcing.md`: how to turn a vague lead into a precise `google_search`, find the right LinkedIn profile, and read a person/company well enough to act. Explorium DB first; this is what you reach for when the DB can't search a criterion or has no data.*

---

## 1. Google dorking — turn a vague query precise

`google_search` supports operators. Combine them to cut noise:

- **Exact phrase:** `"head of revops"` — match the wording verbatim.
- **OR / exclude:** `(cto OR "vp engineering")`, `series-a -recruiting`.
- **Site / domain:** `site:linkedin.com/in`, `site:linkedin.com/company`, `site:crunchbase.com`.
- **In title / url / text:** `intitle:"about us"`, `inurl:careers`, `intext:"series a"`.
- **File type:** `filetype:pdf "investor update"`.
- **Date filter:** `after:2025-01-01`, `before:2026-01-01` for recency windows.
- **Proximity / wildcard:** `term1 AROUND(5) term2`, `"* raised $* series a"`.

**Lead-research dork patterns that work:**
- Find a company's LinkedIn: `site:linkedin.com/company "<company name>"`.
- Find a person at a company: `site:linkedin.com/in "<name>" "<company>"`.
- Find decision makers by role: `site:linkedin.com/in ("CTO" OR "VP Engineering") "<company>"`.
- Funding/buying signals: `"<company>" ("raised" OR "series") after:2025-06-01`.
- Hiring signal: `"<company>" (careers OR "we're hiring") "<role>"`.

Fuzzy-match names/companies — minor spelling differences are fine (`rachit@clodexa.com` ≈ "Rachit Sharma, Clodexa"). Don't attribute a personal-email domain (gmail, outlook) as the company.

## 2. Finding & cleaning a LinkedIn profile

- If you already have a LinkedIn URL, **clean it** to the canonical form before using:
  - person: `…/in/aman-gupta-7217a515/recent-activity/all/` → `…/in/aman-gupta-7217a515/`
  - company: `…/company/wingify/posts/?feedView=all` → `…/company/wingify/`
- If you don't have it: search LinkedIn for *name + company* (People tab) or *company name* (Company tab), open the right result, and confirm it's the correct entity before trusting it.
- If LinkedIn is blocked/empty, fall back to web (dorking above) — a profile, blog, talk, or news mention. Put any non-LinkedIn profile URL you settle on in the `linkedin_url`/profile field. If nothing is found, that's fine — record "not found", don't fabricate.

**What to extract from a person:** name, title, email (only if on the page), profile URL, location, current company domain, list of current companies, photo URL. For each current company: name, LinkedIn URL, logo, `still_working=true`.
**What to extract from a company:** name, LinkedIn URL, logo, plus (from About/Home) HQ, founded, employee count, industry, size.

## 3. Persona analysis (for outreach hooks)

When you need to *understand* a prospect before drafting (handoff to `jobs/outbound_sales_engine.md`):

- Open their profile. Identify the **current** role (the "Experience" entry marked *Present* — this is the employment-verification step from `jobs/lead_sourcing.md` Phase 4).
- Read 10–15 recent posts/reposts/comments. Note the **nature** of activity (post vs repost vs like) and **content** (launch, hiring, fundraise, rant, motivational). Look for fresh signals: raised money, launching a product, hiring.
- Summarise in ~400–500 tokens: who they are, what they post about, their apparent intent/tone. This is the raw material for a genuine, specific connection hook — never a generic template filler.

For a **company** persona, do the same over the company page: Overview/About + 30–40 recent posts → a tight read of what they ship, who they hire, what they announce.

---

**Discipline:** cross-reference at least 2 independent sources for any fact that matters (a funding round, a current role). Qualify claims ("based on their LinkedIn…") rather than asserting. Never invent figures, dates, or URLs. This playbook feeds evidence into `jobs/lead_sourcing.md` Phase 3/4 — keep the bar at *positive evidence*, and file the deep findings to the palace.
