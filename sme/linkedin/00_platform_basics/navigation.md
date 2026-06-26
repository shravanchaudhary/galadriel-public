# LinkedIn Navigation & Core Surfaces

Understanding the structural layouts and primary surfaces of LinkedIn is essential for building a high-fidelity mental model of how an automated agent or assistant navigates the platform.

```
                  +-----------------------------------+
                  |           Navigation Bar          |
                  | [Home] [My Network] [Jobs] [Msg]  |
                  | [Notifications] [Me / Profile]    |
                  +-----------------+-----------------+
                                    |
         +--------------------------+--------------------------+
         |                          |                          |
+--------v--------+        +--------v--------+        +--------v--------+
|    Home Feed    |        |    My Network   |        |    Messaging    |
| - Share updates |        | - Manage invites|        | - 1-on-1 chats  |
| - Read posts    |        | - 1st/2nd/3rd   |        | - InMail inbox  |
| - Engage/React  |        | - Pages/Groups  |        | - Attach docs   |
+-----------------+        +-----------------+        +-----------------+
```

## The Core Surfaces

### 1. Home Feed
*   **URL:** `https://www.linkedin.com/feed/`
*   **Role:** The centralized recommendation engine where posts from immediate connections, followed members, newsletters, and sponsored/recommended content are aggregated.
*   **Key Actions:** Creating posts, uploading documents/images/videos, liking, commenting, reposting (with/without thoughts), and voting on polls.

### 2. My Network
*   **URL:** `https://www.linkedin.com/mynetwork/`
*   **Role:** The relationship dashboard.
*   **Key Actions:** Managing received and sent connection invitations, reviewing suggested people to connect with based on mutual networks, viewing followed Pages, Newsletters, Groups, and Events.

### 3. Jobs
*   **URL:** `https://www.linkedin.com/jobs/`
*   **Role:** The employment marketplace.
*   **Key Actions:** Searching jobs, setting job alerts, tracking applications, and researching company hiring trends.

### 4. Messaging
*   **URL:** `https://www.linkedin.com/messaging/`
*   **Role:** Conversational interface.
*   **Key Actions:** 1-on-1 synchronous and asynchronous communication, managing the Focused vs. Other inbox, sending attachments, voice messages, and InMails.

### 5. Notifications
*   **URL:** `https://www.linkedin.com/notifications/`
*   **Role:** Engagement tracker.
*   **Key Actions:** Monitoring profile views, post reactions/comments, mentions, connection acceptances, and company updates.

### 6. Me (Profile & Account)
*   **URL:** `https://www.linkedin.com/in/[username]/`
*   **Role:** Profile management and settings.
*   **Key Actions:** Modifying security preferences, updating profile contents, tracking analytics, and toggling creator settings.

### 7. Search Bar
*   **Role:** Multi-faceted query interface.
*   **Key Actions:** Executing queries filtered by People, Jobs, Posts, Companies, Schools, Groups, and Events. Supports Boolean operators (`AND`, `OR`, `NOT`) and exact phrase matching (`"double quotes"`).
