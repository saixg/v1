from app.models import Repo


def test_importable():
    assert Repo is not None
