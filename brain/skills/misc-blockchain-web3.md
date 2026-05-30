# Blockchain & Web3 (Unified)

## Goal
Develop secure smart contracts and Web3 applications with proper security patterns and vulnerability prevention.

## When to Use
- Writing Solidity smart contracts
- Auditing contracts for vulnerabilities
- Implementing DeFi protocols
- Building NFT marketplaces
- Integrating Web3 into applications
- Testing smart contracts

## Critical Vulnerabilities

### 1. Reentrancy Attack

The attacker calls back into your contract before state is updated.

**Vulnerable Code:**
```solidity
// VULNERABLE TO REENTRANCY
contract VulnerableBank {
    mapping(address => uint256) public balances;

    function withdraw() public {
        uint256 amount = balances[msg.sender];
        
        // DANGER: External call before state update
        (bool success, ) = msg.sender.call{value: amount}("");
        require(success);
        
        balances[msg.sender] = 0;  // Too late!
    }
}
```

**Secure Pattern (Checks-Effects-Interactions):**
```solidity
contract SecureBank {
    mapping(address => uint256) public balances;

    function withdraw() public {
        uint256 amount = balances[msg.sender];
        require(amount > 0, "Insufficient balance");

        // EFFECTS: Update state BEFORE external call
        balances[msg.sender] = 0;

        // INTERACTIONS: External call last
        (bool success, ) = msg.sender.call{value: amount}("");
        require(success, "Transfer failed");
    }
}
```

**Using ReentrancyGuard:**
```solidity
import "@openzeppelin/contracts/security/ReentrancyGuard.sol";

contract SecureBank is ReentrancyGuard {
    mapping(address => uint256) public balances;

    function withdraw() public nonReentrant {
        uint256 amount = balances[msg.sender];
        require(amount > 0, "Insufficient balance");
        
        balances[msg.sender] = 0;
        
        (bool success, ) = msg.sender.call{value: amount}("");
        require(success, "Transfer failed");
    }
}
```

### 2. Access Control

**Vulnerable Code:**
```solidity
// VULNERABLE: Anyone can call critical functions
contract VulnerableContract {
    address public owner;

    function withdraw(uint256 amount) public {
        // No access control!
        payable(msg.sender).transfer(amount);
    }
}
```

**Secure Pattern:**
```solidity
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/access/AccessControl.sol";

contract SecureContract is Ownable, AccessControl {
    bytes32 public constant ADMIN_ROLE = keccak256("ADMIN_ROLE");
    bytes32 public constant OPERATOR_ROLE = keccak256("OPERATOR_ROLE");

    constructor() {
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(ADMIN_ROLE, msg.sender);
    }

    function adminOnlyFunction() public onlyRole(ADMIN_ROLE) {
        // Only admins
    }

    function operatorFunction() public onlyRole(OPERATOR_ROLE) {
        // Only operators
    }

    function withdraw(uint256 amount) public onlyOwner {
        payable(owner()).transfer(amount);
    }
}
```

### 3. Integer Overflow/Underflow

```solidity
// Solidity 0.8+ has built-in overflow checks
// For < 0.8.0, use SafeMath
