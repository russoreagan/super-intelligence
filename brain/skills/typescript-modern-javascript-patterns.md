# Modern JavaScript Patterns

Comprehensive guide for mastering modern JavaScript (ES6+) features, functional programming patterns, and best practices for writing clean, maintainable, and performant code.

## When to Use This Skill

- Refactoring legacy JavaScript to modern syntax
- Implementing functional programming patterns
- Optimizing JavaScript performance
- Writing maintainable and readable code
- Working with asynchronous operations
- Building modern web applications
- Migrating from callbacks to Promises/async-await
- Implementing data transformation pipelines

## ES6+ Core Features

### 1. Arrow Functions

**Syntax and Use Cases:**
```javascript
// Traditional function
function add(a, b) {
  return a + b;
}

// Arrow function
const add = (a, b) => a + b;

// Single parameter (parentheses optional)
const double = x => x * 2;

// No parameters
const getRandom = () => Math.random();

// Multiple statements (need curly braces)
const processUser = user => {
  const normalized = user.name.toLowerCase();
  return { ...user, name: normalized };
};

// Returning objects (wrap in parentheses)
const createUser = (name, age) => ({ name, age });
```

**Lexical 'this' Binding:**
```javascript
class Counter {
  constructor() {
    this.count = 0;
  }

  // Arrow function preserves 'this' context
  increment = () => {
    this.count++;
  };

  // Traditional function loses 'this' in callbacks
  incrementTraditional() {
    setTimeout(function() {
      this.count++;  // 'this' is undefined
    }, 1000);
  }

  // Arrow function maintains 'this'
  incrementArrow() {
    setTimeout(() => {
      this.count++;  // 'this' refers to Counter instance
    }, 1000);
  }
}
```

### 2. Destructuring

**Object Destructuring:**
```javascript
const user = {
  id: 1,
  name: 'John Doe',
  email: 'john@example.com',
  address: {
    city: 'New York',
    country: 'USA'
  }
};

// Basic destructuring
const { name, email } = user;

// Rename variables
const { name: userName, email: userEmail } = user;

// Default values
const { age = 25 } = user;

// Nested destructuring
const { address: { city, country } } = user;

// Rest operator
const { id, ...userWithoutId } = user;

// Function parameters
function greet({ name, age = 18 }) {
  console.log(`Hello ${name}, you are ${age}`);
}
greet(user);
```

**Array Destructuring:**
```javascript
const numbers = [1, 2, 3, 4, 5];

// Basic destructuring
const [first, second] = numbers;

// Skip elements
const [, , third] = numbers;

// Rest operator
const [head, ...tail] = numbers;

// Swapping variables
let a = 1, b = 2;
[a, b] = [b, a];

// Function return values
function getCoordinates() {
  return [10, 20];
}
const [x, y] = getCoordinates();

// Default values
const [one, two, three = 0] = [1, 2];
```

### 3. Spread and Rest Operators

**Spread Operator:**
```javascript
// Array spreading
const arr1 = [1, 2, 3];
const arr2 = [4, 5, 6];
const combined = [...arr1, ...arr2];

// Object spreading
const defaults = { theme: 'dark', lang: 'en' };
const userPrefs = { theme: 'light' };
const settings = { ...defaults, ...userPrefs };

// Function arguments
const numbers = [1, 2, 3];
Math.max(...numbers);
