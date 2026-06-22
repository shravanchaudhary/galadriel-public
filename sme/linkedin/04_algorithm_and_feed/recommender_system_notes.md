# LinkedIn Feed: Generative Recommender (GR) Sequential Ranking

The ranking stage determines the exact order of posts a member sees. LinkedIn utilizes a state-of-the-art **Generative Recommender (GR)** sequential model to capture professional learning journeys rather than treating impressions independently.

## 1. Deep Sequential Representation

The GR model processes over **1,000 historical interactions** chronologically.

### Causal Attention Transformer
*   The model interleaves post representations with specific user actions (long dwells, likes, comments, shares, skips) as ordered sequence pairs.
*   These interleaved pairs pass through multiple transformer layers with **causal attention** (tokens can only attend to past positions).
*   The self-attention mechanism dynamically weighs interactions: recent activities are prioritized, but relevant posts from weeks ago can be re-weighted if recent signals suggest a renewed professional interest.

---

## 2. Late Fusion and Multitask Heads

Running full self-attention over all features is computationally prohibitive. LinkedIn engineers resolved this with a **Late Fusion** architecture.

```
+-----------------------------------------------------------------+
|                         GR LATE FUSION                          |
+-----------------------------------------------------------------+
|                                                                 |
|   +--------------------------+    +-------------------------+   |
|   | Interleaved Post-Action  |    | Context Features        |   |
|   | Sequences (1000+ Items)  |    | - Viewer device type    |   |
|   |                          |    | - Demographic embedding |   |
|   |                          |    | - Aggregated count /    |   |
|   |                          |    |   affinity signals      |   |
|   +------------+-------------+    +------------+------------+   |
|                |                               |                |
|                v                               |                |
|   +------------+-------------+                 |                |
|   |  Causal Transformer      |                 |                |
|   |  Layers (Self-Attention) |                 |                |
|   +------------+-------------+                 |                |
|                |                               |                |
|                \----------------------+--------/                |
|                                       |                         |
|                                       v                         |
|                           Concatenate / Late Fusion             |
|                                       |                         |
|                                       v                         |
|                           Multi-gate Mixture-of-                    |
|                           Experts (MMoE) Task Head                  |
|                                       |                         |
|                     /-----------------+-----------------\       |
|                    v                                     v      |
|           Active Tasks Head                     Passive Tasks Head   |
|           (Like, Comment, Share)                (Click, Skip, Dwell) |
|                                                                 |
+-----------------------------------------------------------------+
```

### The Late Fusion Strategy
*   **The Concept:** Inject static, non-sequential features (device type, profile embeddings, aggregated historical count/affinity features) **after** sequence processing.
*   **The Advantage:** Avoids quadratic cost inflation over features whose value comes from independent signal strength, not chronological sequence interaction.

### Multi-gate Mixture-of-Experts (MMoE)
*   The fused representations are processed by an MMoE prediction head utilizing shared Deep & Cross Network (DCNv2) experts.
*   **Task-Specific Gating:** Experts are dynamically gated per task:
    *   *Passive Tasks:* Click, Skip, Long-Dwell.
    *   *Active Tasks:* Like, Comment, Share.

---

## 3. Engineering for Production Scale

Deploying massive transformer models with billions of parameters requires substantial GPU optimization to maintain sub-second feed load times.

### Training Optimizations
*   **Custom C++ Data Loader:** Eliminates Python multiprocessing overhead by fusing padding, batching, and packing operations at the native C++ level.
*   **Custom CUDA Kernels:** Designed specifically for multi-label Area Under Curve (AUC) computation, dropping metric evaluation times to negligible overhead.
*   **Checkpoint Parallelization:** Evaluates all checkpoints in parallel across GPUs, significantly speeding up model training and hyperparameter tuning iteration.

### Serving Optimizations (GRMIS)
*   **Disaggregated Architecture:** Separates CPU-bound feature extraction from heavy GPU inference.
*   **Shared Context Batching:** Computes the member's history representation only once, scoring all 2,000 candidates in parallel using custom attention masks.
*   **GRMIS (Generative Recommender Multi-Item Scoring):** A custom **Flash Attention** variant designed specifically for multi-item sequential recommendation scoring, delivering an additional **2x speedup** over PyTorch's native scaled dot-product attention kernels.
