from app.knowledge import api_connection


def test_importable():
    assert api_connection is not None
