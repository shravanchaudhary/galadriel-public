# Outreach Playbook: Anti-Spam & Account Safety Rules

Since LinkedIn heavily prioritizes account safety and has zero tolerance for spammy, automated bot behaviors, keeping campaigns strictly compliant with platform guidelines is the absolute highest priority.

## Account Safety Guidelines & Safeguards

```
              +-----------------------------------+
              |    LinkedIn Safety Guidelines     |
              +-----------------+-----------------+
                                |
       +------------------------+------------------------+
       |                                                 |
+------v------+                                   +------v------+
| Commercial  |                                   |  Behavioral |
|  Limits     |                                   |  Guardrails |
+------+------+                                   +------+------+
       |                                                 |
       |-- Weekly Invites: ~100-200                      |-- Draft-only mode (No auto-clicks)
       |-- Sg. Nav Search: 100 pages max                 |-- Natural human delays (30-90s)
       |-- No scraping profile arrays                    |-- Keep pending invites below 500
```

### 1. Platform Invitation Rules
*   **Weekly Invites:** Limit outgoing connection requests to **100-200 per week**.
*   **Pending Invites Backlog:** Keep total pending sent invitations below **500**. If you exceed this, withdraw the oldest invitations to protect account health.
*   **Spam Markings:** If more than **1-2% of recipients** click "I don't know this person" or "Report Spam" upon receiving your invite, LinkedIn will restrict your account.

### 2. Commercial Search & Sourcing Limits
*   **Commercial Search Limit:** Free accounts face a dynamic limit on search results. Once hit, search views are restricted. Sales Navigator/Premium is mandatory for sustained prospecting.
*   **Search Page Restrictions:** In Sales Navigator, avoid scraping or clicking beyond the 100th search page in a single day, as this triggers automated rate-limiting flags.

### 3. Behavioral Guardrails for Agents
When building a LinkedIn co-pilot or agent, follow these hard architectural rules:

*   **Draft-Only Mode (User in the Loop):**
    *   The agent should only research and draft personalized connection notes, InMails, or comments.
    *   The user must manually approve and click "Send". **Never allow the agent to auto-click or automate browser-level outreach**.
*   **Avoid Raw Scraping:**
    *   Do not scrape large arrays of profile details rapidly. Use official APIs where possible, or browse profiles at a natural human speed.
*   **Simulate Natural Human Delays:**
    *   Add random, natural-looking delays (30 to 90 seconds) between actions (e.g., viewing a profile, saving a lead, drafting a message) to avoid platform telemetry flags.
*   **Respect Rate-Limiting Responses:**
    *   If the platform returns a `429 Too Many Requests` status, immediately pause all activities for a minimum of 24 hours.
