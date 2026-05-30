# Language-Specific Testing Patterns (Unified)

## Goal
Write effective, maintainable tests using language-specific idioms and best practices for Python, JavaScript, Go, and Rust.

## When to Use
- Writing unit tests for new features
- Setting up test fixtures and mocks
- Creating integration tests
- Implementing TDD/BDD practices
- Optimizing test performance

## Python (pytest)

### Basic Test Structure
```python
# tests/test_user_service.py
import pytest
from myapp.services import UserService
from myapp.models import User

class TestUserService:
    """Test suite for UserService."""

    def test_create_user_success(self):
        """Should create a user with valid data."""
        service = UserService()
        user = service.create(name="Alice", email="alice@example.com")
        
        assert user.id is not None
        assert user.name == "Alice"
        assert user.email == "alice@example.com"

    def test_create_user_invalid_email(self):
        """Should raise ValueError for invalid email."""
        service = UserService()
        
        with pytest.raises(ValueError, match="Invalid email"):
            service.create(name="Alice", email="invalid")
```

### Fixtures
```python
# tests/conftest.py
import pytest
from myapp.database import Database
from myapp.models import User

@pytest.fixture
def db():
    """Provide test database connection."""
    database = Database(":memory:")
    database.initialize()
    yield database
    database.close()

@pytest.fixture
def user(db):
    """Provide test user."""
    return User.create(db, name="Test User", email="test@example.com")

@pytest.fixture
def auth_client(user):
    """Provide authenticated test client."""
    from myapp.testing import TestClient
    client = TestClient()
    client.authenticate(user)
    return client

# Using fixtures
def test_get_profile(auth_client, user):
    response = auth_client.get(f"/users/{user.id}")
    assert response.status_code == 200
    assert response.json()["name"] == user.name
```

### Parametrized Tests
```python
@pytest.mark.parametrize("input_value,expected", [
    ("hello", "HELLO"),
    ("world", "WORLD"),
    ("Hello World", "HELLO WORLD"),
    ("", ""),
])
def test_uppercase(input_value, expected):
    assert input_value.upper() == expected

@pytest.mark.parametrize("email,is_valid", [
    ("user@example.com", True),
    ("user@subdomain.example.com", True),
    ("invalid", False),
    ("@example.com", False),
    ("user@", False),
])
def test_email_validation(email, is_valid):
    from myapp.validators import is_valid_email
    assert is_valid_email(email) == is_valid
```

### Mocking
```python
from unittest.mock import Mock, patch, AsyncMock
import pytest

def test_external_api_call():
    with patch("myapp.services.requests.get") as mock_get:
        mock_get.return_value.json.return_value = {"id": 1, "name": "Alice"}
        mock_get.return_value.status_code = 200
        
        result = fetch_user(1)
        
        assert result["name"] == "Alice"
        mock_get.assert_called_once_with("https://api.example.com/users/1")
