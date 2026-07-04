# Cookbook: Accept LinkedIn Invites & Filter Leads/Investors

**Goal:** Process incoming LinkedIn connection requests. Accept standard requests, but flag high-value profiles (Investors / Inbound Leads) for Shravan's review.

## Steps

1. **Navigate to Invitations:**
   - `browser("open https://www.linkedin.com/mynetwork/invitation-manager/")`
   - Run `browser("state")` to view the list of pending connection requests.

2. **Evaluate Each Invitation:**
   - For each profile, look at their headline, current role, and any attached note.
   - **Is it an Investor?** (Keywords: "VC", "Partner", "Venture Capital", "Angel Investor", "Seed", "Capital").
   - **Is it an Inbound Lead?** (Attached note mentions "Clodexa", "AI agents", "demo", "your product", or "sales intelligence").

3. **Take Action:**
   - **If Investor or Inbound Lead:** DO NOT accept autonomously. Log the exact name, headline, and note in today's progress file (`state/progress/`) with the flag `[REQUIRES SHRAVAN'S REVIEW]`.
   - **If Standard/General:** Click the "Accept" button for that index.

4. **Completion:**
   - Once the page of invites is cleared, write a brief summary to today's progress file (e.g., "Accepted 4 standard invites. Flagged 1 investor: [Name]").
   - Mark as `done_pending_verify`.
