# LinkedIn Feed: Algorithmic Retrieval & Ranking Summary

The LinkedIn Feed serves over 1.3 billion professionals, executing one of the largest-scale recommendation systems in the industry. The feed ranking system has transitioned from isolated, pointwise predictions to a hybrid, next-generation relevance architecture powered by Large Language Models (LLMs), sequential recommenders, and GPU-accelerated infrastructure.

```
                      +-----------------------------+
                      |         User Opens          |
                      |        LinkedIn Feed        |
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      |     1. Unified Retrieval    |
                      | - Shared Dual Encoder LLM   |
                      | - Candidate Generation      |
                      | - ~2,000 top candidates     |
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      |       2. Sequential GR      |
                      | - Causal Attention Transf.  |
                      | - Processes 1,000+ history  |
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      |       3. Late Fusion        |
                      | - Injects count/affinities  |
                      | - Gated MMoE Head           |
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      |     4. GPU Online Serving   |
                      | - Sub-50ms K-NN search      |
                      | - Sub-second Feed Render    |
                      +-----------------------------+
```

## The Architecture Overview

The next-generation LinkedIn feed separates recommendation into two core stages to meet millisecond latency constraints:

### 1. Unified Retrieval (Candidate Generation)
*   **The Goal:** Screen down hundreds of millions of prospective posts to approximately **2,000 highly relevant candidates** under sub-50ms constraints.
*   **Traditional Method:** Relied on multiple disparate, complex retrieval pipelines (network chron, collaborative filtering, topic-based trending indexing).
*   **Modern Method:** A unified retrieval system powered by dual-encoder LLM embeddings. It captures deep semantic and professional relationships beyond shallow keyword matches, resolving the critical "cold-start" problem for new members.

### 2. Sequential Ranking (Generative Recommender - GR)
*   **The Goal:** Sort and score the 2,000 retrieved candidates to determine the exact sequence of posts displayed on the member's feed.
*   **Traditional Method:** Evaluated each impression independently in isolation (pointwise).
*   **Modern Method:** Treats the member's interaction history as an ordered sequence of professional actions (a continuous learning journey), processing over 1,000 historical interactions through a causal transformer model. This captures the user's professional trajectory and mindset shifts in near-real-time.
