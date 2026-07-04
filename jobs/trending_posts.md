# Trending Post Extraction Engine

## Overview
This cookbook manages the daily execution of our "Seed List" strategy. Since native LinkedIn hashtag search returns low-engagement noise, we brute-force extract posts from the recent-activity feeds of highly proven B2B/SaaS creators. 

We apply strict engagement thresholds to ensure only true, top-performing posts make it into the database for content modeling.

## The Strategy

1. **The Seed Roster:** Hardcoded list of top creators.
2. **The Extraction Loop:** Visit their feed, scrape the text, likes, and comments. 
3. **The Filters:** 
   - `likes >= 1000` (Engagement floor).
   - "Blindness" Check: Drop posts that rely on images or are less than a full paragraph (since we can't 'read' the image hook).
4. **The Action:** Insert into DB via `db_create(entity='trending_post')` in state `discovered`.

## 1. The Seed List

*Target ~10 creators per daily loop to avoid rate limits.*

- Justin Welsh (`https://www.linkedin.com/in/justinwelsh/recent-activity/all/`)
- Jason Lemkin (`https://www.linkedin.com/in/jasonmlemkin/recent-activity/all/`)
- Alex Hormozi (`https://www.linkedin.com/in/alexhormozi/recent-activity/all/`)
- Matt Gray (`https://www.linkedin.com/in/mattgray1/recent-activity/all/`)
- Sahil Lavingia (`https://www.linkedin.com/in/sahillavingia/recent-activity/all/`)
- [Add more top B2B/SaaS founders over time...]

## 2. Daily Patrol Instructions (For the Worker)

**Frequency:** Once daily (e.g., 9:00 AM).

**Step-by-step:**
1. Pick 5-10 creators from the list above.
2. For each creator:
   - `browser("open https://www.linkedin.com/in/<creator_slug>/recent-activity/all/ && state")`
   - Scroll down 1-2 times to load recent posts.
   - Extract the text, likes, and comments.
   - Run the two filters (Engagement >= 1000, Text is self-contained).
   - If a post passes:
     - `db_create` the entity `trending_post` with the extracted data. Use the post URL (or creator_name + timestamp) as the unique `post_url` key.

## 3. Analysis Loop

Once posts sit in the `discovered` state:
1. `db_query(entity="trending_post", filter={"status": "discovered"})`
2. Run LLM analysis on the text to extract the structural DNA (hook format, pacing, core psychological angle).
3. Record the analysis in a new `post_draft` workflow (which we will build next) or directly transition `db_move_state(entity='trending_post', to='analyzing')` to track progress.
4. Move to `curated` when done, or `rejected` if upon closer look it's not usable.