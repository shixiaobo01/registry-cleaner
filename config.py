"""Built-in defaults.

For customer delivery, use an external config.json (see config.example.json)
instead of placing Registry addresses or credentials in this file.
"""

REGISTRY_URL = "https://registry.example.com"
USERNAME = ""
PASSWORD_ENV = "REGISTRY_PASSWORD"

# Set False only for a trusted test registry with a self-signed certificate.
# A path to a CA certificate file may also be used.
VERIFY_TLS = False
REQUEST_TIMEOUT_SECONDS = 30
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 1.0

CATALOG_PAGE_SIZE = 1000
TAGS_PAGE_SIZE = 1000

KEEP_LAST = 3
# If an image config has no `created` field, use the Registry blob's HTTP
# Last-Modified timestamp. Tags with neither timestamp are retained for safety.
FALLBACK_TO_LAST_MODIFIED = True
SKIP_REPOSITORY_PREFIXES = ("tools/", "base/", "cicd/")
SKIP_REPOSITORIES = set()  # Exact repository names to never scan.

# These tags are never candidates for deletion.  Glob patterns are supported.
PROTECTED_TAG_PATTERNS = ()

# An outer worker scans repositories; each one uses tag workers for metadata.
# Total simultaneous requests can approach their product, so start conservatively.
REPOSITORY_WORKERS = 2
TAG_WORKERS = 12

CHECKPOINT_FILE = "checkpoint.json"
LOG_FILE = "cleaner.log"
CSV_FILE = "registry-cleaner-actions.csv"
TAG_COUNTS_FILE = "registry-tag-counts.csv"
