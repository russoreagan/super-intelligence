# MikoPBX API Test Generating

Generate comprehensive Python pytest tests for MikoPBX REST API endpoints with full parameter coverage, schema validation, and edge case testing.

## What This Skill Does

Analyzes DataStructure.php files and generates complete pytest test suites including:
- ✅ CRUD operation tests (Create, Read, Update, Delete)
- ✅ Positive and negative test cases
- ✅ Parameter validation tests
- ✅ Edge cases and boundary conditions
- ✅ Schema validation tests
- ✅ Proper fixtures and authentication
- ✅ Detailed assertions with error messages

## When to Use This Skill

Use this skill when you need to:
- Create pytest tests for new REST API endpoints
- Add comprehensive test coverage for existing endpoints
- Generate tests covering all parameter combinations
- Add schema validation tests for API responses
- Create edge case and negative tests
- Ensure API compliance with OpenAPI specification

## Quick Start

### Basic Usage

When the user requests test generation:

1. **Identify the endpoint**
   - API path (e.g., `/pbxcore/api/v3/extensions`)
   - HTTP methods (GET, POST, PUT, DELETE, PATCH)
   - Resource name (e.g., Extensions)

2. **Locate DataStructure.php**
   ```bash
   find /Users/nb/PhpstormProjects/mikopbx/Core/src/PBXCoreREST/Lib -name "DataStructure.php" | grep -i "{resource}"
   ```

3. **Analyze parameter definitions**
   Extract from `DataStructure.php`:
   - Required vs optional parameters
   - Data types and validation rules
   - Default values
   - Enum values
   - Pattern constraints (regex)
   - Min/max values

4. **Generate test file**
   Use the complete template from [test-template.py](templates/test-template.py)

5. **Customize for endpoint**
   - Replace `{ResourceName}` placeholders
   - Fill in actual payload structures
   - Add specific field validations
   - Include enum and pattern validations

## Test Structure

### File Organization

```python
tests/api/
├── test_{resource}_api.py        # Main test file
└── conftest.py                   # Shared fixtures
```

### Test Class Structure

Each test file should have these test classes:

```python
class TestCreate{ResourceName}:
    """Test POST endpoint for creating resources"""
    - test_create_with_valid_data()
    - test_create_missing_required_field()
    - test_create_with_invalid_type()

class TestGet{ResourceName}:
    """Test GET endpoint for retrieving resources"""
    - test_get_all()
    - test_get_by_id()
    - test_get_nonexistent()

class TestUpdate{ResourceName}:
    """Test PUT/PATCH endpoints for updating resources"""
    - test_update_with_valid_data()
    - test_patch_partial_update()

class TestDelete{ResourceName}:
    """Test DELETE endpoint for removing resources"""
    - test_delete_existing()
    - test_delete_nonexistent()

class TestSchemaValidation{ResourceName}:
    """Test response schema validation"""
    - test_response_matches_openapi_schema()

class TestEdgeCases{ResourceName}:
    """Test edge cases and boundary conditions"""
    - test_special_characters_in_fields()
    - test_empty_string_values()
    - test_boundary_values()
```

### Standard Fixtures
