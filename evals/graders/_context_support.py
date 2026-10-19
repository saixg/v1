"""Shared support for `with_without_context.py`: path/env setup (so that
module can import `app.*` the same way `citations.py`/`graph_sanity.py` do),
a disposable scanned fixture repo, and the pass/fail reporting shape the
other graders already use.

Leading underscore: this is a local helper for this directory's graders, not
a public module.
"""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
API_SRC = REPO_ROOT / "services" / "api"
sys.path.insert(0, str(API_SRC))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

import psycopg  # noqa: E402

from app.knowledge import create_project, create_repo, create_task, worker_connection  # noqa: E402
from app.models import Repo, Task  # noqa: E402

# A repo small enough to hand-check, big enough to give each of
# with_without_context.py's five tasks real files to find: an import chain
# (api -> orders -> store), a pricing helper, a refund path that imports a
# notifier, a migration and a vendored file (so the "do not touch" scopes in
# the corpus name real paths, not placeholders), and a README for the
# baseline. Eleven files, matching the module's own "eleven source files"
# accounting.
FILES: dict[str, str] = {
    "README.md": (
        "# orders-service\n\n"
        "A small internal service that accepts carts, places orders, applies\n"
        "percentage discount codes at checkout, and issues refunds with an\n"
        "email notification to the customer.\n\n"
        "## Layout\n\n"
        "- `svc/orders.py` — cart validation and order placement.\n"
        "- `svc/store.py` — persistence of order rows.\n"
        "- `svc/pricing.py` — discount-code math, kept free of I/O.\n"
        "- `svc/api.py` — the HTTP-facing checkout handler.\n"
        "- `svc/refunds.py` / `svc/notify.py` — refund issuance and its email.\n"
        "- `migrations/` — schema history; never edit an existing file in here.\n"
        "- `vendor/` — a third-party shipping estimator; treated as read-only.\n\n"
        "## Running tests\n\n"
        "The test suite covers order placement and refund issuance. Run it\n"
        "the same way as the rest of this repository's services.\n"
    ),
    "svc/__init__.py": "",
    "svc/store.py": (
        "import logging\n"
        "\n"
        "logger = logging.getLogger(__name__)\n"
        "\n"
        "_NEXT_ID = 1\n"
        "\n"
        "\n"
        "def save_order(order):\n"
        '    """Write one order row to the orders table and return it with an id.\n\n'
        "    The caller is expected to have already computed the order's total;\n"
        "    this function performs no pricing logic of its own, only storage.\n"
        "    A monotonically increasing id is assigned here rather than by the\n"
        "    caller, since id assignment is this module's one responsibility.\n"
        '    """\n'
        "    global _NEXT_ID\n"
        "    order = dict(order)\n"
        "    order['id'] = _NEXT_ID\n"
        "    _NEXT_ID += 1\n"
        "    logger.info('order %s saved with total %s', order['id'], order.get('total'))\n"
        "    return order\n"
        "\n"
        "\n"
        "def find_order(order_id):\n"
        '    """Look up a previously saved order by id.\n\n'
        "    Stubbed for this fixture: a real backend would issue a select\n"
        "    against the orders table and raise if no row matched. Returning\n"
        "    None here always means 'not found', never 'not yet loaded'.\n"
        '    """\n'
        "    return None\n"
        "\n"
        "\n"
        "def update_order_total(order, new_total):\n"
        '    """Overwrite a saved order\'s total, used only by pricing fixes\n'
        '    applied after the order was already placed."""\n'
        "    order = dict(order)\n"
        "    order['total'] = new_total\n"
        "    return order\n"
    ),
    "svc/orders.py": (
        "from svc.store import save_order\n"
        "\n"
        "\n"
        "def compute_total(cart):\n"
        '    """Sum the price of every line item in the cart.\n\n'
        "    Quantities default to 1 when a line item omits one, matching\n"
        "    how the storefront's cart widget serializes a single-add.\n"
        '    """\n'
        "    return sum(item['price'] * item.get('quantity', 1) for item in cart)\n"
        "\n"
        "\n"
        "def validate_cart(cart):\n"
        '    """Raise ValueError for a cart that cannot be priced: empty, or\n'
        '    containing a line item with a negative or missing price."""\n'
        "    if not cart:\n"
        "        raise ValueError('cart is empty')\n"
        "    for item in cart:\n"
        "        if item.get('price', -1) < 0:\n"
        "            raise ValueError('cart contains an item with no valid price')\n"
        "\n"
        "\n"
        "def place_order(cart):\n"
        '    """Validate a cart and persist it as an order.\n\n'
        "    An empty cart is rejected before any write happens: there is\n"
        "    nothing meaningful to charge or ship, and allowing it through\n"
        "    would create an order row with a zero total that downstream\n"
        "    reporting cannot distinguish from a pricing bug.\n"
        '    """\n'
        "    validate_cart(cart)\n"
        "    total = compute_total(cart)\n"
        "    return save_order({'items': cart, 'total': total})\n"
        "\n"
        "\n"
        "def cancel_order(order):\n"
        '    """Mark a saved order as cancelled. Not used by checkout; kept\n'
        '    here so refund logic has a path to cancel instead of refund."""\n'
        "    order = dict(order)\n"
        "    order['status'] = 'cancelled'\n"
        "    return order\n"
    ),
    "svc/pricing.py": (
        "# Percentage discount codes, checked at checkout time only. A code not\n"
        "# in this table is treated as unrecognized rather than an error, so an\n"
        "# expired or mistyped code degrades to full price instead of a 500.\n"
        "DISCOUNT_RATES = {\n"
        "    'SAVE10': 0.10,\n"
        "    'SAVE20': 0.20,\n"
        "    'WELCOME5': 0.05,\n"
        "}\n"
        "\n"
        "MAX_DISCOUNT_RATE = max(DISCOUNT_RATES.values())\n"
        "\n"
        "\n"
        "def apply_discount(total, code):\n"
        '    """Reduce `total` by a percentage discount code.\n\n'
        "    Deliberately free of I/O (no database lookup, no network call):\n"
        "    every caller in this service needs this to be a pure function it\n"
        "    can call synchronously inside a request handler, and a future\n"
        "    caller must not be tempted to add a lookup here.\n"
        '    """\n'
        "    rate = DISCOUNT_RATES.get(code, 0.0)\n"
        "    return round(total * (1 - rate), 2)\n"
        "\n"
        "\n"
        "def is_valid_code(code):\n"
        '    """Whether `code` is a discount code this service recognizes."""\n'
        "    return code in DISCOUNT_RATES\n"
        "\n"
        "\n"
        "def best_possible_total(total):\n"
        '    """The lowest total `total` could ever become under any code\n'
        '    this service currently recognizes. Used by a pricing sanity\n'
        '    check elsewhere in the stack, not by checkout itself."""\n'
        "    return round(total * (1 - MAX_DISCOUNT_RATE), 2)\n"
    ),
    "svc/api.py": (
        "from svc.orders import place_order\n"
        "from svc.pricing import apply_discount\n"
        "\n"
        "\n"
        "def handle_checkout(request):\n"
        '    """Place an order for an incoming checkout request, applying a\n'
        "    discount code when one is present and valid. The public name of\n"
        "    this handler is relied on by the frontend's checkout button and\n"
        "    must not change.\n"
        '    """\n'
        "    order = place_order(request['cart'])\n"
        "    code = request.get('discount_code')\n"
        "    if code:\n"
        "        order['total'] = apply_discount(order['total'], code)\n"
        "    return order\n"
        "\n"
        "\n"
        "def handle_refund_request(request):\n"
        '    """Thin wrapper kept here so the refund route has a stable entry\n'
        '    point even though the real logic lives in svc.refunds."""\n'
        "    from svc.refunds import handle_refund\n"
        "\n"
        "    return handle_refund(request['order_id'], request['amount'])\n"
    ),
    "svc/test_orders.py": (
        "from svc.orders import place_order\n"
        "\n"
        "\n"
        "def test_place_order():\n"
        "    assert place_order([{'price': 10}])['total'] == 10\n"
        "\n"
        "\n"
        "def test_place_order_rejects_empty_cart():\n"
        "    try:\n"
        "        place_order([])\n"
        "    except ValueError:\n"
        "        return\n"
        "    raise AssertionError('expected ValueError for an empty cart')\n"
    ),
    "svc/notify.py": (
        "SENDER_ADDRESS = 'orders@example.invalid'\n"
        "\n"
        "\n"
        "def send_refund_email(order_id, amount):\n"
        '    """Email the customer that their refund of `amount` for `order_id`\n'
        "    was issued. A production implementation would call out to the\n"
        "    transactional-email provider; this fixture only records the call,\n"
        "    which is why there is no retry or delivery-failure handling here.\n"
        '    """\n'
        "    return True\n"
        "\n"
        "\n"
        "def send_order_confirmation(order_id):\n"
        '    """Email the customer that their order was placed. Not used by\n'
        '    the refund flow, kept here because both emails share a sender."""\n'
        "    return True\n"
        "\n"
        "\n"
        "def send_shipping_delay_notice(order_id, new_eta):\n"
        '    """Email the customer that their order\'s estimated arrival moved.\n'
        '    Unrelated to refunds; included so this module reads like the\n'
        '    service\'s one real notification sender rather than a single-use\n'
        '    helper."""\n'
        "    return True\n"
    ),
    "svc/refunds.py": (
        "from svc.notify import send_refund_email\n"
        "from svc.store import find_order\n"
        "\n"
        "REFUND_REASONS = ('damaged', 'wrong_item', 'customer_changed_mind', 'other')\n"
        "\n"
        "\n"
        "def handle_refund(order_id, amount):\n"
        '    """Issue a refund for order_id and notify the customer exactly\n'
        "    once. Looking the order up first (even though this fixture's\n"
        "    stub always returns None) mirrors the real service, where a\n"
        "    refund for an unknown order should fail before any email sends.\n"
        '    """\n'
        "    find_order(order_id)\n"
        "    send_refund_email(order_id, amount)\n"
        "    return {'order_id': order_id, 'amount': amount}\n"
        "\n"
        "\n"
        "def is_known_reason(reason):\n"
        '    """Whether `reason` is one of this service\'s recognized refund\n'
        '    reasons, used to validate a reason before it is stored."""\n'
        "    return reason in REFUND_REASONS\n"
    ),
    "svc/test_refunds.py": (
        "from svc.refunds import handle_refund\n"
        "\n"
        "\n"
        "def test_handle_refund():\n"
        "    assert handle_refund('o1', 10) == {'order_id': 'o1', 'amount': 10}\n"
        "\n"
        "\n"
        "def test_handle_refund_is_idempotent_per_call():\n"
        "    first = handle_refund('o2', 5)\n"
        "    second = handle_refund('o2', 5)\n"
        "    assert first == second\n"
    ),
    "migrations/0001_create_orders.sql": (
        "-- Initial schema for the orders table. Do not edit this file once it\n"
        "-- has shipped: add a new migration instead, so every environment\n"
        "-- that already applied this one stays consistent with every other.\n"
        "create table orders (\n"
        "    id integer primary key,\n"
        "    total integer not null,\n"
        "    discount_code text,\n"
        "    refund_reason text,\n"
        "    created_at timestamp not null default now()\n"
        ");\n"
    ),
    "vendor/shipping.py": (
        "# Vendored from the shipping-estimates package. Do not edit anything\n"
        "# under vendor/: changes here are overwritten the next time this\n"
        "# package is re-vendored from upstream, and a local fix would silently\n"
        "# disappear on the next vendor refresh with no warning to whoever made it.\n"
        "\n"
        "\n"
        "def estimate_shipping(cart):\n"
        '    """Flat-rate shipping estimate for a cart. Upstream\'s real\n'
        "    implementation calls a carrier API; this vendored copy's test\n"
        '    double always returns zero."""\n'
        "    return 0\n"
        "\n"
        "\n"
        "def supported_carriers():\n"
        '    """Carriers this vendored module knows how to quote."""\n'
        "    return ['standard', 'express', 'overnight']\n"
        "\n"
        "\n"
        "def estimate_delivery_days(cart, carrier):\n"
        '    """Upstream\'s stubbed delivery estimate, in days, for `carrier`.\n'
        '    Unused by svc/ today; present because upstream ships it."""\n'
        "    return {'standard': 5, 'express': 2, 'overnight': 1}.get(carrier, 5)\n"
    ),
}


def db_available() -> bool:
    return bool(os.environ.get("WORKER_DATABASE_URL") and os.environ.get("DATABASE_URL"))


def _context_engine_schema_present() -> bool:
    with worker_connection() as conn:
        row = conn.execute(
            "select 1 from information_schema.tables "
            "where table_schema = 'codeaxis' and table_name = 'context_packages'"
        ).fetchone()
    return row is not None


def skip_reason() -> str | None:
    if not db_available():
        return "WORKER_DATABASE_URL / DATABASE_URL not set"
    if not _context_engine_schema_present():
        return (
            "codeaxis.context_packages does not exist — apply "
            "supabase/migrations/20261020000000_context_engine.sql"
        )
    return None


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


def _scan_into(repo_id: str, commit_sha: str, root: Path) -> None:
    """Run the shipped scan pass over `root`, with embeddings forced off
    (no key in the environment) so chunks record `embedding_not_configured`
    deterministically rather than depending on a live provider call."""
    from app.analysis import jobs

    key = os.environ.pop("VOYAGE_API_KEY", None)
    try:
        jobs.run_scan_pass(repo_id, commit_sha, str(root))
    finally:
        if key is not None:
            os.environ["VOYAGE_API_KEY"] = key


@dataclass(frozen=True)
class ScannedFixture:
    """A disposable, scanned repo plus the project/profile it lives under.
    Pair with `delete_fixture` in the caller's teardown."""

    user_id: str
    project_id: str
    repo_id: str
    commit_sha: str
    root: Path

    def new_task(self, intent: str, *, title: str) -> Task:
        with worker_connection() as conn:
            task = create_task(
                conn,
                Task(
                    project_id=self.project_id,
                    repo_id=self.repo_id,
                    commit_sha=self.commit_sha,
                    title=title,
                    intent=intent,
                    created_by=self.user_id,
                ),
            )
        assert task.id is not None
        return task


def scan_fixture(name: str, root: Path | None = None) -> ScannedFixture:
    """A fresh profile, project and repo, scanned once from an in-memory
    file tree keyed by `name` (only "orders-service" exists today)."""
    if name != "orders-service":
        raise ValueError(f"no such fixture: {name!r}")

    owner_url = os.environ["DATABASE_URL"]
    user_id = f"user_eval_{uuid.uuid4().hex}"
    with psycopg.connect(owner_url, connect_timeout=10) as conn:
        conn.execute(
            "insert into codeaxis.profiles (id, email) values (%s, %s)",
            (user_id, f"{user_id}@codeaxis.invalid"),
        )
        conn.commit()

    import tempfile

    workdir = root or Path(tempfile.mkdtemp(prefix="codeaxis-eval-"))
    _write_tree(workdir, FILES)

    sha = f"eval-{uuid.uuid4().hex[:12]}"
    with worker_connection() as conn:
        project = create_project(conn, f"with/without context eval ({name})", user_id)
        repo = create_repo(conn, Repo(project_id=project.id, local_path=str(workdir)))
    assert repo.id is not None
    _scan_into(repo.id, sha, workdir)
    return ScannedFixture(user_id, project.id, repo.id, sha, workdir)


def delete_fixture(fx: ScannedFixture) -> None:
    """Graph, chunk, convention and context-engine tables have no cascade
    from repos, so they go first, then the repo, project and profile."""
    owner_url = os.environ["DATABASE_URL"]
    with psycopg.connect(owner_url, connect_timeout=10) as conn:
        conn.execute("delete from codeaxis.context_packages where repo_id = %s", (fx.repo_id,))
        conn.execute("delete from codeaxis.tasks where repo_id = %s", (fx.repo_id,))
        for table in ("chunks", "conventions", "summaries", "blobs", "edges", "nodes"):
            conn.execute(f"delete from codeaxis.{table} where repo_id = %s", (fx.repo_id,))
        for table in ("coverage", "pass_runs"):
            conn.execute(f"delete from codeaxis.{table} where repo_id = %s", (fx.repo_id,))
        conn.execute("delete from codeaxis.repos where id = %s", (fx.repo_id,))
        conn.execute("delete from codeaxis.projects where id = %s", (fx.project_id,))
        conn.execute("delete from codeaxis.profiles where id = %s", (fx.user_id,))
        conn.commit()


# --------------------------------------------------------------- reporting


class Checks:
    """Accumulates pass/fail assertions without raising, so a grader can run
    every check and report all failures in one pass instead of stopping at
    the first."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            self.failures.append(message)

    @property
    def ok(self) -> bool:
        return not self.failures


def report(section: str, checks: Checks, metrics: dict[str, object]) -> bool:
    ok = checks.ok
    print(f"[{'PASS' if ok else 'FAIL'}] {section}")
    for key, value in metrics.items():
        print(f"    {key}: {value}")
    for failure in checks.failures:
        print(f"    - {failure}")
    return ok
