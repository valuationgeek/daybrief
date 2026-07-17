"""
fusion.py — Stage 4: Cluster related articles by semantic similarity,
then generate a unified cross-source summary per cluster.

Fix #7: fuse_all() now clusters across ALL categories, not per-category.
        A story appearing in both "world" and "breaking" is deduplicated.
Fix #6: cluster dict now carries article URLs so templates can link them.
"""

import uuid
import logging
from collections import Counter
from datetime import datetime

from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from .analyzer import _strip_meta
from .llm import generate

log = logging.getLogger("daybrief.fusion")

_embed_model = None


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        log.info("Loading sentence-transformer model...")
        _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _embed_model


def _embed_articles(articles):
    model = _get_embed_model()
    texts = [
        f"{a['title']}. {a.get('summary') or a.get('body', '')[:300]}"
        for a in articles
    ]
    return model.encode(texts, batch_size=32, show_progress_bar=False)


def _cluster_articles(embeddings, threshold=0.78):
    """
    Greedy single-linkage clustering across all articles.
    threshold lowered slightly from 0.82 → 0.78 to catch cross-category
    duplicates which often use slightly different wording.
    Returns list of clusters, each a list of article indices.
    """
    n        = len(embeddings)
    assigned = [-1] * n
    clusters = []
    cid      = 0
    sim_mat  = cosine_similarity(embeddings)

    for i in range(n):
        if assigned[i] != -1:
            continue
        cluster     = [i]
        assigned[i] = cid
        for j in range(i + 1, n):
            if assigned[j] == -1 and sim_mat[i][j] >= threshold:
                cluster.append(j)
                assigned[j] = cid
        clusters.append(cluster)
        cid += 1

    return clusters


def _fuse_cluster(cluster_articles):
    """Generate a unified summary for a multi-source cluster via LLM."""
    sources  = ", ".join({a["source"] for a in cluster_articles})
    combined = "\n\n".join([
        f"Source: {a['source']}\n{a.get('summary') or a.get('body', '')[:500]}"
        for a in cluster_articles
    ])

    prompt = (
        f"Multiple news sources ({sources}) are covering the same event.\n"
        "Write a unified 3-4 sentence summary that:\n"
        "1. Describes the core event using only facts stated in the sources\n"
        "2. Notes key facts agreed on by multiple sources\n"
        "3. Mentions any notable differences in framing between sources\n"
        "4. States the significance — do NOT invent figures or names not in the sources\n"
        "5. Refers to people and their titles or roles exactly as the sources do — "
        "do NOT add background knowledge (e.g. \"former\", \"current\", job titles, "
        "historical context) that is not stated in the sources\n\n"
        f"Articles:\n{combined[:3000]}\n\nUnified summary:"
    )

    unified = generate(prompt, max_tokens=300)
    if unified:
        return _strip_meta(unified)

    # LLM unavailable — fall back to joining the individual summaries
    parts = [a.get("summary", "") for a in cluster_articles if a.get("summary")]
    return " | ".join(parts[:3])


def fuse_all(articles):
    """
    Cluster ALL articles together regardless of category.
    This deduplicates stories that appear in multiple categories
    (e.g. same event in both 'world' and 'breaking').

    Returns:
      - updated articles list (cluster_id set on each article)
      - list of cluster dicts (only clusters with 2+ articles)
    """
    if len(articles) < 2:
        return articles, []

    log.info(f"Clustering {len(articles)} articles across all categories...")
    embeddings   = _embed_articles(articles)
    raw_clusters = _cluster_articles(embeddings)
    cluster_dicts = []
    today = datetime.utcnow().date().isoformat()

    for raw_cluster in raw_clusters:
        cid              = str(uuid.uuid4())[:8]
        cluster_articles = [articles[i] for i in raw_cluster]

        for i in raw_cluster:
            articles[i]["cluster_id"] = cid

        if len(cluster_articles) < 2:
            continue  # solo article — no fusion needed

        log.info(
            f"Cluster {cid}: {len(cluster_articles)} articles "
            f"({', '.join(a['source'] for a in cluster_articles)})"
        )

        unified = _fuse_cluster(cluster_articles)

        all_kw        = [kw for a in cluster_articles for kw in a.get("keywords", [])]
        top_kw        = [kw for kw, _ in Counter(all_kw).most_common(10)]
        avg_score     = sum(a.get("score") or 0 for a in cluster_articles) / len(cluster_articles)
        cluster_score = min(1.0, avg_score + 0.05 * len(cluster_articles))

        # Determine display category: use the most common category in the cluster
        cat_counts = Counter(a["category"] for a in cluster_articles)
        display_cat = cat_counts.most_common(1)[0][0]

        cluster_dicts.append({
            "cluster_id":      cid,
            "run_date":        today,
            "category":        display_cat,   # category for display grouping
            "categories":      list(cat_counts.keys()),  # all categories covered
            "article_ids":     [a["id"]     for a in cluster_articles],
            "source_count":    len(cluster_articles),
            "sources":         [a["source"] for a in cluster_articles],
            "titles":          [a["title"]  for a in cluster_articles],
            "urls":            [a["url"]    for a in cluster_articles],  # Fix #6
            "unified_summary": unified,
            "top_keywords":    top_kw,
            "score":           round(cluster_score, 4),
            "flags":           [],
        })

    log.info(f"{len(cluster_dicts)} multi-source clusters formed across all categories")
    return articles, cluster_dicts
