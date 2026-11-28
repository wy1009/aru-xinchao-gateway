"""Prepare current search vectors, resuming only uncovered source revisions."""
import argparse
import time
from collections import Counter
from .search import KINDS, chunks, fill, models
from .store import setup


def main():
    from serein.config import load_settings
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    settings = load_settings(args.config)
    setup(settings.database)
    profile, embed, _ = models(settings)
    corpus = chunks(settings.database, KINDS)
    from serein.adapters.embedding import EmbeddingTransientError
    for attempt in range(5):
        try:
            count = fill(settings.database,profile,embed,corpus)
            break
        except EmbeddingTransientError as error:
            if attempt == 4:
                raise
            time.sleep(min(60,max(2 ** attempt, error.retry_after or 0)))
    print('indexed',count,'current passages',dict(Counter(r[0] for r in corpus.values())),flush=True)


if __name__ == '__main__':
    main()
