"""Table-driven test of classify_layer against representative paths for
each rule, plus an unmatched path that must return None rather than a
guessed layer."""

import pytest

from app.analysis.architecture.layers import classify_layer

CASES = [
    # api
    ("app/routes/users.py", "api"),
    ("src/controllers/UserController.ts", "api"),
    ("services/api/app/main.py", "api"),
    ("src/userRouter.ts", "api"),
    ("src/order-endpoint.ts", "api"),
    # data
    ("app/models/user.py", "data"),
    ("app/schemas/order.py", "data"),
    ("src/entities/Account.ts", "data"),
    # service
    ("services/payments/client.py", "service"),
    # test
    ("tests/test_users.py", "test"),
    ("app/test_users.py", "test"),
    ("src/__tests__/users.ts", "test"),
    ("src/users.test.ts", "test"),
    ("src/users.spec.ts", "test"),
    # config
    ("config/settings.py", "config"),
    ("app/settings.py", "config"),
    (".env.local", "config"),
    ("webpack.config.js", "config"),
    # infra
    ("infra/sandbox/Dockerfile", "infra"),
    ("docker/Dockerfile", "infra"),
    ("terraform/main.tf", "infra"),
    (".github/workflows/ci.yml", "infra"),
    # ui -- frontend-extension-scoped so a Python app/ package doesn't match
    ("apps/web/src/components/Button.tsx", "ui"),
    ("apps/web/src/pages/index.tsx", "ui"),
    ("apps/web/src/app/layout.tsx", "ui"),
    # unmatched: no rule applies, must be None not a guess
    ("README.md", None),
    ("scripts/setup_db_roles.py", None),
    ("app/analysis/architecture/writer.py", None),
]


@pytest.mark.parametrize(("path", "expected"), CASES)
def test_classify_layer(path: str, expected: str | None) -> None:
    assert classify_layer(path) == expected


def test_python_app_package_not_misclassified_as_ui() -> None:
    # A Python app/ package (no frontend extension) must not become "ui"
    # just because it's under a path containing "app/".
    assert classify_layer("app/knowledge/writes.py") is None
