---
name: language-testing-patterns
description: Use when writing tests in Python (pytest), JavaScript (Jest/Vitest), Go, or Rust. Covers unit testing, mocking, fixtures, integration tests, and language-specific testing idioms.
summary: Testing patterns for Python (pytest), JavaScript (Jest/Vitest), Go (testing), and Rust with mocking, fixtures, and integration tests.
triggers: [pytest, Jest, Vitest, Go testing, Rust testing, unit test, mock, fixture, integration test]
disable-model-invocation: true

---
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

@pytest.mark.asyncio
async def test_async_service():
    with patch("myapp.services.AsyncClient.get", new_callable=AsyncMock) as mock:
        mock.return_value.json = AsyncMock(return_value={"data": "test"})
        
        result = await async_fetch_data()
        
        assert result["data"] == "test"

# Mock as fixture
@pytest.fixture
def mock_email_service():
    with patch("myapp.services.EmailService") as mock:
        mock.return_value.send.return_value = True
        yield mock.return_value
```

### Async Testing
```python
import pytest
import asyncio

@pytest.mark.asyncio
async def test_async_function():
    result = await async_fetch_data()
    assert result is not None

@pytest.mark.asyncio
async def test_concurrent_requests():
    results = await asyncio.gather(
        fetch_user(1),
        fetch_user(2),
        fetch_user(3),
    )
    assert len(results) == 3
```

## JavaScript (Jest/Vitest)

### Basic Test Structure
```typescript
// user.service.test.ts
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { UserService } from './user.service';

describe('UserService', () => {
  let service: UserService;

  beforeEach(() => {
    service = new UserService();
  });

  it('should create a user with valid data', async () => {
    const user = await service.create({
      name: 'Alice',
      email: 'alice@example.com',
    });

    expect(user.id).toBeDefined();
    expect(user.name).toBe('Alice');
    expect(user.email).toBe('alice@example.com');
  });

  it('should throw for invalid email', async () => {
    await expect(
      service.create({ name: 'Alice', email: 'invalid' })
    ).rejects.toThrow('Invalid email');
  });
});
```

### Mocking
```typescript
import { vi, describe, it, expect, beforeEach } from 'vitest';

// Mock module
vi.mock('./api-client', () => ({
  ApiClient: vi.fn().mockImplementation(() => ({
    get: vi.fn().mockResolvedValue({ data: { id: 1, name: 'Alice' } }),
    post: vi.fn().mockResolvedValue({ data: { success: true } }),
  })),
}));

// Mock function
const mockFetch = vi.fn();
global.fetch = mockFetch;

describe('API calls', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('should fetch user data', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ id: 1, name: 'Alice' }),
    });

    const user = await fetchUser(1);

    expect(user.name).toBe('Alice');
    expect(mockFetch).toHaveBeenCalledWith('/api/users/1');
  });
});

// Spy on methods
it('should call console.log', () => {
  const spy = vi.spyOn(console, 'log');
  
  myFunction();
  
  expect(spy).toHaveBeenCalledWith('expected message');
  spy.mockRestore();
});
```

### React Component Testing
```typescript
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { LoginForm } from './LoginForm';

describe('LoginForm', () => {
  it('should submit form with credentials', async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    
    render(<LoginForm onSubmit={onSubmit} />);

    await user.type(screen.getByLabelText(/email/i), 'user@example.com');
    await user.type(screen.getByLabelText(/password/i), 'password123');
    await user.click(screen.getByRole('button', { name: /login/i }));

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith({
        email: 'user@example.com',
        password: 'password123',
      });
    });
  });

  it('should show error for invalid email', async () => {
    const user = userEvent.setup();
    
    render(<LoginForm onSubmit={vi.fn()} />);

    await user.type(screen.getByLabelText(/email/i), 'invalid');
    await user.click(screen.getByRole('button', { name: /login/i }));

    expect(screen.getByText(/invalid email/i)).toBeInTheDocument();
  });
});
```

## Go Testing

### Basic Test Structure
```go
// user_service_test.go
package service

import (
    "testing"
    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
)

func TestUserService_Create(t *testing.T) {
    service := NewUserService()
    
    user, err := service.Create("Alice", "alice@example.com")
    
    require.NoError(t, err)
    assert.NotEmpty(t, user.ID)
    assert.Equal(t, "Alice", user.Name)
    assert.Equal(t, "alice@example.com", user.Email)
}

func TestUserService_Create_InvalidEmail(t *testing.T) {
    service := NewUserService()
    
    _, err := service.Create("Alice", "invalid")
    
    assert.Error(t, err)
    assert.Contains(t, err.Error(), "invalid email")
}
```

### Table-Driven Tests
```go
func TestEmailValidation(t *testing.T) {
    tests := []struct {
        name     string
        email    string
        expected bool
    }{
        {"valid email", "user@example.com", true},
        {"valid with subdomain", "user@sub.example.com", true},
        {"missing @", "userexample.com", false},
        {"missing domain", "user@", false},
        {"empty string", "", false},
    }

    for _, tt := range tests {
        t.Run(tt.name, func(t *testing.T) {
            result := IsValidEmail(tt.email)
            assert.Equal(t, tt.expected, result)
        })
    }
}
```

### Mocking with Interfaces
```go
// Define interface for dependency
type EmailSender interface {
    Send(to, subject, body string) error
}

// Mock implementation
type MockEmailSender struct {
    mock.Mock
}

func (m *MockEmailSender) Send(to, subject, body string) error {
    args := m.Called(to, subject, body)
    return args.Error(0)
}

func TestNotificationService_SendWelcome(t *testing.T) {
    mockSender := new(MockEmailSender)
    mockSender.On("Send", "user@example.com", "Welcome!", mock.Anything).Return(nil)
    
    service := NewNotificationService(mockSender)
    
    err := service.SendWelcome("user@example.com")
    
    assert.NoError(t, err)
    mockSender.AssertExpectations(t)
}
```

### HTTP Testing
```go
func TestHandler_GetUser(t *testing.T) {
    // Create test server
    handler := NewUserHandler(mockService)
    
    req := httptest.NewRequest("GET", "/users/123", nil)
    w := httptest.NewRecorder()
    
    handler.ServeHTTP(w, req)
    
    assert.Equal(t, http.StatusOK, w.Code)
    
    var response User
    err := json.NewDecoder(w.Body).Decode(&response)
    require.NoError(t, err)
    assert.Equal(t, "123", response.ID)
}
```

## Rust Testing

### Basic Tests
```rust
// src/user.rs
pub struct User {
    pub id: String,
    pub name: String,
    pub email: String,
}

impl User {
    pub fn new(name: &str, email: &str) -> Result<Self, String> {
        if !email.contains('@') {
            return Err("Invalid email".to_string());
        }
        Ok(User {
            id: uuid::Uuid::new_v4().to_string(),
            name: name.to_string(),
            email: email.to_string(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_create_user_success() {
        let user = User::new("Alice", "alice@example.com").unwrap();
        
        assert!(!user.id.is_empty());
        assert_eq!(user.name, "Alice");
        assert_eq!(user.email, "alice@example.com");
    }

    #[test]
    fn test_create_user_invalid_email() {
        let result = User::new("Alice", "invalid");
        
        assert!(result.is_err());
        assert_eq!(result.unwrap_err(), "Invalid email");
    }
}
```

### Parameterized Tests (with rstest)
```rust
use rstest::rstest;

#[rstest]
#[case("hello", "HELLO")]
#[case("world", "WORLD")]
#[case("Rust", "RUST")]
fn test_uppercase(#[case] input: &str, #[case] expected: &str) {
    assert_eq!(input.to_uppercase(), expected);
}

#[rstest]
#[case("user@example.com", true)]
#[case("invalid", false)]
#[case("", false)]
fn test_email_validation(#[case] email: &str, #[case] expected: bool) {
    assert_eq!(is_valid_email(email), expected);
}
```

### Async Tests
```rust
#[tokio::test]
async fn test_async_fetch() {
    let client = TestClient::new();
    
    let result = client.fetch_user(1).await.unwrap();
    
    assert_eq!(result.name, "Alice");
}

#[tokio::test]
async fn test_concurrent_requests() {
    let client = TestClient::new();
    
    let (user1, user2) = tokio::join!(
        client.fetch_user(1),
        client.fetch_user(2),
    );
    
    assert!(user1.is_ok());
    assert!(user2.is_ok());
}
```

### Mocking with mockall
```rust
use mockall::predicate::*;
use mockall::*;

#[automock]
trait EmailSender {
    fn send(&self, to: &str, subject: &str, body: &str) -> Result<(), String>;
}

#[test]
fn test_send_welcome_email() {
    let mut mock = MockEmailSender::new();
    mock.expect_send()
        .with(eq("user@example.com"), eq("Welcome!"), always())
        .times(1)
        .returning(|_, _, _| Ok(()));

    let service = NotificationService::new(Box::new(mock));
    
    let result = service.send_welcome("user@example.com");
    
    assert!(result.is_ok());
}
```

## Testing Best Practices

| Practice              | Description                                    |
| --------------------- | ---------------------------------------------- |
| AAA Pattern           | Arrange, Act, Assert structure                 |
| Single Assertion      | One logical assertion per test                 |
| Descriptive Names     | Test names describe expected behavior          |
| Isolated Tests        | No shared state between tests                  |
| Fast Tests            | Mock external dependencies                     |
| Deterministic         | Same input always produces same result         |

## Implementation Checklist
- [ ] Tests follow AAA pattern (Arrange, Act, Assert)
- [ ] Test names describe expected behavior
- [ ] External dependencies mocked
- [ ] Fixtures used for common setup
- [ ] Edge cases covered (null, empty, boundaries)
- [ ] Error conditions tested
- [ ] Async code tested properly
- [ ] Integration tests separate from unit tests
- [ ] CI runs tests on every commit
