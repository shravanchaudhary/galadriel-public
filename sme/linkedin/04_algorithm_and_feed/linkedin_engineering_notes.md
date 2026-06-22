# LinkedIn Feed: Engineering & LLM Retrieval Notes

Detailed technical insights and breakthroughs from the development of LinkedIn's unified retrieval stage, which utilizes fine-tuned Large Language Models (LLMs) to represent members and items.

## 1. Feature Representation & Prompt Engineering

To feed structured profile and engagement data into text-based language models, LinkedIn builds templated sequences known as "prompts":
*   **Post Prompts:** Formats, author details (headline, company, industry), engagement counts, article metadata, and the raw text of the post.
*   **Member Prompts:** Skills, work history, education, and a chronologically ordered sequence of posts they have previously engaged with ("member engagement history").

### The Pop-Percentile Breakthrough (Numerical Feature Engineering)
*   **The Problem:** LLMs do not inherently understand continuous numerical magnitudes. Passing raw popularity features (e.g., `"views:12345"`) resulted in poor tokenization and near-zero correlation (**-0.004**) between post popularity and cosine similarity scores.
*   **The Solution:** Continuous numerical values (views, clicks, engagement rates) are broken into **percentile buckets (1-100)** and wrapped in special, stable tokens (e.g., `<view_percentile>71</view_percentile>`).
*   **The Result:** Percentile values tokenize as a single token, giving the LLM an ordinal vocabulary for quantity. Popularity correlation jumped **30x**, and retrieval **Recall@10 improved by 15%**.

Key insight:

> **Continuous quantities should be mapped into a stable, learnable percentile-bucket vocabulary to optimize LLM comprehension.**

---

## 2. Training Dual Encoders at Scale

The dual-encoder architecture uses a shared, distilled LLM to process member and item prompts independently, comparing output embeddings via cosine similarity.

```
+---------------+                      +---------------+
| Member Prompt |                      |  Post Prompt  |
+-------+-------+                      +-------+-------+
        |                                      |
        v                                      v
+-------+-------+                      +-------+-------+
|  Shared LLM   |                      |  Shared LLM   |
| (Distilled)   |                      | (Distilled)   |
+-------+-------+                      +-------+-------+
        |                                      |
        v                                      v
+-------+-------+                      +-------+-------+
| Member Embed  |                      |  Item Embed   |
+-------+-------+                      +-------+-------+
        \                                      /
         \------------------+-----------------/
                            |
                            v
                    Cosine Similarity
                    (InfoNCE Loss)
```

### Contrastive Negative Sourcing (InfoNCE Loss)
Each positive member-item engagement is contrasted against easy and hard negative samples:
*   **Easy Negatives:** Randomly sampled posts that were not shown to the member (provides a weak, stable contrastive signal).
*   **Hard Negatives:** Posts that were actually displayed (impressed) to the member but received **zero engagement**.
*   **The Impact:** Adding just **2 hard negatives per member improved Recall@10 by +3.6%**:

| Hard Negative Configuration | Recall@10 vs. Baseline |
| :--- | :--- |
| Easy Negatives Only | Baseline |
| Easy + 1 Hard Negative / Member | +2.0% |
| Easy + 2 Hard Negatives / Member | **+3.6%** |

### Context Optimization: Positives-Only History
*   **The Breakthrough:** When constructing the member's engagement history, including full impressions (engaged + scrolled-past) degraded performance and inflated computational memory costs (quadratic context overhead).
*   **The Optimization:** Filtering member history to include **only posts with positive engagement** led to massive efficiency gains on a cluster of 8 H100 GPUs:
    *   **37% reduction** in memory footprint.
    *   Ability to process **40% more sequences** per batch.
    *   **2.6x faster training iteration** due to shorter sequences.

---

## 3. Near-Real-Time Online Serving

To balance freshness with sub-50ms latency, the system is decoupled into three nearline pipelines:
1.  **Prompt Generation:** Constantly updates prompts in key-value stores to capture new posts, profile updates, and real-time member activities.
2.  **Embedding Inference:** Batches fresh prompts and routes them to LLM inference servers running on GPU clusters.
3.  **GPU-Accelerated Indexing:** Ingests item embeddings into a specialized nearest-neighbor index. When a member opens their feed, k-nearest-neighbor search retrieves candidates instantly.
