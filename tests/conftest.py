from pathlib import Path
from uuid import uuid4

import pytest

from robot_agents.config import ROOT


@pytest.fixture
def run_directory(request) -> Path:
    directory = ROOT / ".artifacts" / "tests" / request.node.name / uuid4().hex
    directory.mkdir(parents=True)
    return directory
