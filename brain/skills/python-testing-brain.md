---
name: python-testing-brain
description: pytest invocations, fixture patterns, async testing, and mock patterns for writing test steps. Brain-local reference optimized for planning test commands.
disable-model-invocation: true

---
# Python Testing Patterns — Brain Reference

## Running tests
```bash
python -m pytest tests/                          # run all
python -m pytest tests/test_foo.py              # single file
python -m pytest tests/test_foo.py::test_bar    # single test
python -m pytest -k "keyword"                   # filter by name
python -m pytest -x                             # stop on first failure
python -m pytest -v                             # verbose output
python -m pytest --tb=short                     # compact tracebacks
python -m pytest -s                             # show print output
```

## Fixtures
```python
@pytest.fixture
def client():
    return MyClient()

@pytest.fixture(scope="session")  # scope: function|class|module|session
def db():
    conn = create_connection()
    yield conn          # teardown runs after yield
    conn.close()
```

## Async tests
```python
import pytest

@pytest.mark.asyncio
async def test_something():
    result = await my_async_fn()
    assert result == expected

# conftest.py — enable asyncio mode globally
# pytest.ini or pyproject.toml:
# [tool.pytest.ini_options]
# asyncio_mode = "auto"
```

## Mocking
```python
from unittest.mock import patch, MagicMock, AsyncMock

# Patch a dependency
with patch("module.ClassName") as mock_cls:
    mock_cls.return_value.method.return_value = "value"
    result = code_under_test()

# Async mock
mock_fn = AsyncMock(return_value={"key": "val"})

# Fixture-based patch
@pytest.fixture
def mock_api(mocker):          # requires pytest-mock
    return mocker.patch("module.api_call", return_value={"ok": True})
```

## Parameterization
```python
@pytest.mark.parametrize("input,expected", [
    ("hello", 5),
    ("hi", 2),
])
def test_len(input, expected):
    assert len(input) == expected
```

## Assert patterns
```python
with pytest.raises(ValueError, match="invalid"):
    risky_fn()

assert result == pytest.approx(3.14, abs=0.01)
```
