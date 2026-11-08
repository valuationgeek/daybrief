"""
embed_model.py — The one place the sentence-transformer model is loaded.

Pinned to a Hugging Face commit, so a change upstream (or a tampered repo)
can't swap the weights or code under the pipeline, and loaded with
trust_remote_code off. Fusion and KeyBERT share this instance, so the model
loads once per run.

To move to a newer revision: check the commit on
https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/commits/main,
update REVISION, then run `python -m agent.embed_model` to download it.
"""

import logging
import threading

log = logging.getLogger("daybrief.embed_model")

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"

_model = None
_lock = threading.Lock()   # analysis threads call get_model concurrently
# Hold around every model.encode(): analyzer workers extract keywords from
# several threads, and the model's fast tokenizer isn't thread-safe
# ("RuntimeError: Already borrowed"), which silently left articles untagged.
encode_lock = threading.Lock()


def get_model():
    """The shared SentenceTransformer, from the local cache when it's there."""
    with _lock:
        return _load_once()


def _load_once():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        def load(local_only):
            return SentenceTransformer(
                MODEL_ID,
                revision=REVISION,
                trust_remote_code=False,
                local_files_only=local_only,
            )

        log.info("Loading sentence-transformer model...")
        try:
            _model = load(local_only=True)
        except Exception as e:
            # Not cached yet (fresh setup) or the cache was cleared. Downloading
            # the same pinned revision is still safe.
            log.info(f"Model not in the local cache ({type(e).__name__}); downloading revision {REVISION[:12]}")
            _model = load(local_only=False)
    return _model


if __name__ == "__main__":
    get_model()
    print(f"  Model ready: {MODEL_ID} @ {REVISION[:12]}")
