import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config as config_module
from src import db as db_module


@pytest.fixture()
def cfg():
    return config_module.load_config()


@pytest.fixture()
def conn():
    connection = db_module.connect(":memory:")
    yield connection
    connection.close()
