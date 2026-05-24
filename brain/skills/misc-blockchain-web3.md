---
name: blockchain-web3
description: Use when developing smart contracts with Solidity, building DeFi protocols, or testing blockchain applications. Covers security patterns, common vulnerabilities, and Web3 integration.
summary: Blockchain development with Solidity security patterns, smart contract vulnerabilities, DeFi protocols, and Web3 testing.
triggers: [Solidity, smart contract, blockchain, DeFi, Web3, Ethereum, NFT, ERC-20, security]
disable-model-invocation: true

---
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

import "@openzeppelin/contracts/utils/math/SafeMath.sol";

contract Token {
    using SafeMath for uint256;
    mapping(address => uint256) public balances;

    function transfer(address to, uint256 amount) public {
        // Automatically reverts on overflow/underflow in 0.8+
        balances[msg.sender] -= amount;
        balances[to] += amount;
        
        // For 0.7.x and below:
        // balances[msg.sender] = balances[msg.sender].sub(amount);
        // balances[to] = balances[to].add(amount);
    }
}
```

### 4. Front-Running Protection

```solidity
contract SecureAuction {
    mapping(bytes32 => bool) public commitments;
    mapping(address => uint256) public bids;
    
    // Commit-reveal scheme
    function commitBid(bytes32 commitment) public {
        commitments[commitment] = true;
    }
    
    function revealBid(uint256 amount, bytes32 nonce) public payable {
        bytes32 commitment = keccak256(abi.encodePacked(msg.sender, amount, nonce));
        require(commitments[commitment], "Invalid commitment");
        require(msg.value == amount, "Amount mismatch");
        
        bids[msg.sender] = amount;
        delete commitments[commitment];
    }
}
```

## ERC-20 Token Implementation

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/token/ERC20/extensions/ERC20Burnable.sol";
import "@openzeppelin/contracts/token/ERC20/extensions/ERC20Pausable.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

contract MyToken is ERC20, ERC20Burnable, ERC20Pausable, Ownable {
    constructor(uint256 initialSupply) 
        ERC20("MyToken", "MTK") 
        Ownable(msg.sender) 
    {
        _mint(msg.sender, initialSupply * 10 ** decimals());
    }

    function mint(address to, uint256 amount) public onlyOwner {
        _mint(to, amount);
    }

    function pause() public onlyOwner {
        _pause();
    }

    function unpause() public onlyOwner {
        _unpause();
    }

    function _update(address from, address to, uint256 value)
        internal
        override(ERC20, ERC20Pausable)
    {
        super._update(from, to, value);
    }
}
```

## ERC-721 NFT Implementation

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import "@openzeppelin/contracts/token/ERC721/extensions/ERC721URIStorage.sol";
import "@openzeppelin/contracts/token/ERC721/extensions/ERC721Enumerable.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

contract MyNFT is ERC721, ERC721URIStorage, ERC721Enumerable, Ownable {
    uint256 private _nextTokenId;
    uint256 public constant MAX_SUPPLY = 10000;
    uint256 public mintPrice = 0.01 ether;

    constructor() ERC721("MyNFT", "MNFT") Ownable(msg.sender) {}

    function mint(address to, string memory uri) public payable {
        require(_nextTokenId < MAX_SUPPLY, "Max supply reached");
        require(msg.value >= mintPrice, "Insufficient payment");

        uint256 tokenId = _nextTokenId++;
        _safeMint(to, tokenId);
        _setTokenURI(tokenId, uri);
    }

    function withdraw() public onlyOwner {
        payable(owner()).transfer(address(this).balance);
    }

    // Required overrides
    function _update(address to, uint256 tokenId, address auth)
        internal
        override(ERC721, ERC721Enumerable)
        returns (address)
    {
        return super._update(to, tokenId, auth);
    }

    function _increaseBalance(address account, uint128 value)
        internal
        override(ERC721, ERC721Enumerable)
    {
        super._increaseBalance(account, value);
    }

    function tokenURI(uint256 tokenId)
        public
        view
        override(ERC721, ERC721URIStorage)
        returns (string memory)
    {
        return super.tokenURI(tokenId);
    }

    function supportsInterface(bytes4 interfaceId)
        public
        view
        override(ERC721, ERC721Enumerable, ERC721URIStorage)
        returns (bool)
    {
        return super.supportsInterface(interfaceId);
    }
}
```

## Testing Smart Contracts

### Foundry Tests
```solidity
// test/MyToken.t.sol
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "../src/MyToken.sol";

contract MyTokenTest is Test {
    MyToken public token;
    address public owner;
    address public alice;
    address public bob;

    function setUp() public {
        owner = address(this);
        alice = makeAddr("alice");
        bob = makeAddr("bob");
        
        token = new MyToken(1000000);
    }

    function testInitialSupply() public {
        assertEq(token.totalSupply(), 1000000 * 10 ** token.decimals());
    }

    function testTransfer() public {
        uint256 amount = 1000 * 10 ** token.decimals();
        token.transfer(alice, amount);
        
        assertEq(token.balanceOf(alice), amount);
    }

    function testTransferInsufficientBalance() public {
        vm.prank(alice);
        vm.expectRevert();
        token.transfer(bob, 1000);
    }

    function testFuzz_Transfer(uint256 amount) public {
        vm.assume(amount <= token.balanceOf(owner));
        
        token.transfer(alice, amount);
        assertEq(token.balanceOf(alice), amount);
    }
}
```

### Hardhat Tests
```typescript
// test/MyToken.test.ts
import { expect } from "chai";
import { ethers } from "hardhat";
import { MyToken } from "../typechain-types";
import { SignerWithAddress } from "@nomicfoundation/hardhat-ethers/signers";

describe("MyToken", function () {
  let token: MyToken;
  let owner: SignerWithAddress;
  let alice: SignerWithAddress;
  let bob: SignerWithAddress;

  beforeEach(async function () {
    [owner, alice, bob] = await ethers.getSigners();
    
    const MyToken = await ethers.getContractFactory("MyToken");
    token = await MyToken.deploy(1000000n);
  });

  it("should have correct initial supply", async function () {
    const decimals = await token.decimals();
    const expected = 1000000n * 10n ** decimals;
    expect(await token.totalSupply()).to.equal(expected);
  });

  it("should transfer tokens", async function () {
    const amount = ethers.parseUnits("1000", 18);
    await token.transfer(alice.address, amount);
    expect(await token.balanceOf(alice.address)).to.equal(amount);
  });

  it("should fail transfer with insufficient balance", async function () {
    await expect(
      token.connect(alice).transfer(bob.address, 1000n)
    ).to.be.reverted;
  });
});
```

## Web3 Integration (Frontend)

```typescript
import { ethers } from 'ethers';

// Connect wallet
async function connectWallet() {
  if (!window.ethereum) {
    throw new Error("No wallet found");
  }
  
  const provider = new ethers.BrowserProvider(window.ethereum);
  const accounts = await provider.send("eth_requestAccounts", []);
  const signer = await provider.getSigner();
  
  return { provider, signer, address: accounts[0] };
}

// Interact with contract
async function mintNFT(contractAddress: string, tokenURI: string) {
  const { signer } = await connectWallet();
  
  const contract = new ethers.Contract(
    contractAddress,
    ["function mint(address to, string memory uri) payable"],
    signer
  );
  
  const tx = await contract.mint(
    await signer.getAddress(),
    tokenURI,
    { value: ethers.parseEther("0.01") }
  );
  
  return await tx.wait();
}

// Listen to events
function listenToTransfers(contractAddress: string) {
  const provider = new ethers.BrowserProvider(window.ethereum);
  
  const contract = new ethers.Contract(
    contractAddress,
    ["event Transfer(address indexed from, address indexed to, uint256 value)"],
    provider
  );
  
  contract.on("Transfer", (from, to, value) => {
    console.log(`Transfer: ${from} -> ${to}: ${ethers.formatEther(value)}`);
  });
}
```

## Security Checklist
- [ ] Checks-Effects-Interactions pattern followed
- [ ] ReentrancyGuard on state-changing functions
- [ ] Access control on privileged functions
- [ ] Input validation on all external functions
- [ ] Integer overflow protection (Solidity 0.8+ or SafeMath)
- [ ] External call return values checked
- [ ] No hardcoded addresses in production
- [ ] Emergency pause mechanism implemented
- [ ] Events emitted for state changes
- [ ] Comprehensive test coverage
- [ ] Professional audit before mainnet
