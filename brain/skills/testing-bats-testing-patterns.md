# Bats Testing Patterns

Comprehensive guidance for writing comprehensive unit tests for shell scripts using Bats (Bash Automated Testing System), including test patterns, fixtures, and best practices for production-grade shell testing.

## When to Use This Skill

- Writing unit tests for shell scripts
- Implementing test-driven development (TDD) for scripts
- Setting up automated testing in CI/CD pipelines
- Testing edge cases and error conditions
- Validating behavior across different shell environments
- Building maintainable test suites for scripts
- Creating fixtures for complex test scenarios
- Testing multiple shell dialects (bash, sh, dash)

## Bats Fundamentals

### What is Bats?

Bats (Bash Automated Testing System) is a TAP (Test Anything Protocol) compliant testing framework for shell scripts that provides:
- Simple, natural test syntax
- TAP output format compatible with CI systems
- Fixtures and setup/teardown support
- Assertion helpers
- Parallel test execution

### Installation

```bash
# macOS with Homebrew
brew install bats-core

# Ubuntu/Debian
git clone https://github.com/bats-core/bats-core.git
cd bats-core
./install.sh /usr/local

# From npm (Node.js)
npm install --global bats

# Verify installation
bats --version
```

### File Structure

```
project/
├── bin/
│   ├── script.sh
│   └── helper.sh
├── tests/
│   ├── test_script.bats
│   ├── test_helper.sh
│   ├── fixtures/
│   │   ├── input.txt
│   │   └── expected_output.txt
│   └── helpers/
│       └── mocks.bash
└── README.md
```

## Basic Test Structure

### Simple Test File

```bash
#!/usr/bin/env bats

# Load test helper if present
load test_helper

# Setup runs before each test
setup() {
    export TMPDIR=$(mktemp -d)
}

# Teardown runs after each test
teardown() {
    rm -rf "$TMPDIR"
}

# Test: simple assertion
@test "Function returns 0 on success" {
    run my_function "input"
    [ "$status" -eq 0 ]
}

# Test: output verification
@test "Function outputs correct result" {
    run my_function "test"
    [ "$output" = "expected output" ]
}

# Test: error handling
@test "Function returns 1 on missing argument" {
    run my_function
    [ "$status" -eq 1 ]
}
```

## Assertion Patterns

### Exit Code Assertions

```bash
#!/usr/bin/env bats

@test "Command succeeds" {
    run true
    [ "$status" -eq 0 ]
}

@test "Command fails as expected" {
    run false
    [ "$status" -ne 0 ]
}

@test "Command returns specific exit code" {
    run my_function --invalid
    [ "$status" -eq 127 ]
}

@test "Can capture command result" {
    run echo "hello"
    [ $status -eq 0 ]
    [ "$output" = "hello" ]
}
```

### Output Assertions

```bash
#!/usr/bin/env bats

@test "Output matches string" {
    result=$(echo "hello world")
    [ "$result" = "hello world" ]
}

@test "Output contains substring" {
    result=$(echo "hello world")
    [[ "$result" == *"world"* ]]
}

@test "Output matches pattern" {
    result=$(date +%Y)
    [[ "$result" =~ ^[0-9]{4}$ ]]
}

@test "Multi-line output" {
    run printf "line1\nline2\nline3"
    [ "$output" = "line1
line2
line3" ]
}
