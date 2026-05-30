# API Tester Skill

Quick API endpoint testing with comprehensive request/response validation.

## Instructions

You are an API testing expert. When invoked:

1. **Test API Endpoints**:
   - Validate HTTP methods (GET, POST, PUT, PATCH, DELETE)
   - Test request headers and body formats
   - Verify response status codes
   - Validate response schema and data types
   - Check authentication and authorization

2. **Generate Test Cases**:
   - Create curl commands for testing
   - Generate Postman collections
   - Write automated test scripts
   - Test edge cases and error scenarios
   - Validate API contracts

3. **Performance Testing**:
   - Load testing with concurrent requests
   - Response time benchmarking
   - Rate limit verification
   - Timeout handling
   - Connection pooling tests

4. **Security Testing**:
   - Authentication/authorization checks
   - Input validation testing
   - SQL injection prevention
   - XSS prevention
   - CORS configuration

## Usage Examples

```
@api-tester
@api-tester --endpoint /api/users
@api-tester --method POST
@api-tester --load-test
@api-tester --generate-collection
```

## REST API Testing

### GET Request Examples

#### Basic GET Request
```bash
# curl
curl -X GET https://api.example.com/api/users \
  -H "Content-Type: application/json"

# With authentication
curl -X GET https://api.example.com/api/users \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json"

# With query parameters
curl -X GET "https://api.example.com/api/users?page=1&limit=10&sort=created_at" \
  -H "Authorization: Bearer YOUR_TOKEN"

# Verbose output (includes headers)
curl -v -X GET https://api.example.com/api/users
```

#### JavaScript/Node.js
```javascript
// Using fetch
async function getUsers() {
  const response = await fetch('https://api.example.com/api/users', {
    method: 'GET',
    headers: {
      'Authorization': 'Bearer YOUR_TOKEN',
      'Content-Type': 'application/json'
    }
  });

  if (!response.ok) {
    throw new Error(`HTTP error! status: ${response.status}`);
  }

  const data = await response.json();
  return data;
}

// Using axios
const axios = require('axios');

async function getUsers() {
  try {
    const response = await axios.get('https://api.example.com/api/users', {
      headers: {
        'Authorization': 'Bearer YOUR_TOKEN'
      },
      params: {
        page: 1,
        limit: 10
      }
    });
    return response.data;
  } catch (error) {
    console.error('Error:', error.response?.data || error.message);
    throw error;
  }
}
```

#### Python
```python
import requests

# Basic GET request
response = requests.get('https://api.example.com/api/users')
print(response.json())

# With authentication and parameters
headers = {
    'Authorization': 'Bearer YOUR_TOKEN',
    'Content-Type': 'application/json'
}

params = {
    'page': 1,
    'limit': 10,
    'sort': 'created_at'
}

response = requests.get(
    'https://api.example.com/api/users',
    headers=headers,
    params=params
)

if response.status_code == 200:
    data = response.json()
    print(data)
else:
    print(f"Error: {response.status_code}")
    print(response.text)
```

### POST Request Examples
