"""cs.AI top-cited papers tool.

Fetches arXiv cs.AI paper metadata and citation counts (Semantic Scholar /
OpenAlex), stores them in SQLite, and reports the top-k most-cited papers per
calendar month. Citation counts can be refreshed on demand.
"""

__version__ = "0.1.0"
