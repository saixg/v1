import shutil

import pytest


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker not installed")
def test_docker_available():
    assert shutil.which("docker")
