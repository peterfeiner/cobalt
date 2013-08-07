import pytest

from . import setup, teardown

@pytest.fixture(scope='session', autouse=True)
def database(request):
    setup()
    request.addfinalizer(teardown)
    
