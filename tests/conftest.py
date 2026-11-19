import pytest

import generate_data
from finpulse.db import connect


@pytest.fixture(scope="session")
def dataset():
    return generate_data.build_dataset()


@pytest.fixture(scope="session")
def conn(dataset):
    """A connection to a database freshly loaded with the generated books."""
    with connect() as connection:
        generate_data.load(connection, dataset)
        yield connection
